import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader
import os
import csv
import datetime
from sklearn.model_selection import train_test_split

from TEEGM.data.datasets import preprocess, k_fold_generator, EEG_Dataset, seed_torch, window_split
from TEEGM.models.IFNet import IFNet
from TEEGM.utils.engine import train, retrain, validate, evaluate
from TEEGM.utils.tools import Compose
from TEEGM.optimizer import build_optimizer
from TEEGM.data.repeated_trial_augmentation import RepeatedTrialAugmentation
from TEEGM.data.random_erasing import RandomErasing
from TEEGM.data.random_crop import RandomCrop
from TEEGM.data.cutmix import CutMix

from yacs.config import CfgNode
from config import get_config



def build_lr_scheduler(optimizer, config, n_iter_per_epoch):
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, n_iter_per_epoch * config.TRAIN.EPOCHS,
                                                           eta_min=config.TRAIN.BASE_LR * (10 ** -2))
    return scheduler


def build_datasets_files(config, stage='train'):
    datasets = []  # target files for each subject
    target_file = config.DATA.TRAIN_FILES if stage == 'train' else config.DATA.TEST_FILES
    for dir in sorted(os.listdir(config.DATA.DATA_PATH)):
        if '.' in dir:
            continue
        data_files = []
        for file in sorted(os.listdir(config.DATA.DATA_PATH + dir)):
            if file in target_file:
                data_files.append(config.DATA.DATA_PATH + dir + '/' + file)
        if data_files:
            datasets.append(data_files)
    return datasets


def build_tranforms(config):
    return Compose([
        RandomCrop(config.MODEL.TIME_POINTS),
        CutMix(),  #used in IFNet V2
        RandomErasing(),
    ])


def build_retrainer(config, train_x, train_y, val_x, val_y, model):
    trainval_x = torch.cat((train_x, val_x), dim=0)
    trainval_y = torch.cat((train_y, val_y), dim=0)
    trainval_loader = DataLoader(EEG_Dataset(trainval_x, trainval_y), batch_size=config.DATA.BATCH_SIZE,
                              shuffle=True, num_workers=0, drop_last=True)
    val_loader = DataLoader(EEG_Dataset(*window_split(trainval_x, trainval_y, config)), batch_size=config.DATA.BATCH_SIZE * 4,
                              shuffle=False, num_workers=0, )

    optimizer = build_optimizer(model, config)
    scheduler = None
    return {'trainval_loader':trainval_loader, 'val_loader':val_loader, 'optimizer':optimizer, 'scheduler':scheduler}


def train_model(config, train_x, train_y, val_x=None, val_y=None, subject=''):
    transform = build_tranforms(config)
    rta = RepeatedTrialAugmentation(transform, m=config.DATA.RTA)

    print(train_x.shape), print(train_y.shape)
    print(val_x.shape), print(val_y.shape)
    train_x, train_y, val_x, val_y = torch.from_numpy(train_x).cuda(), torch.from_numpy(train_y).long().cuda(), \
                                     torch.from_numpy(val_x).cuda(), torch.from_numpy(val_y).long().cuda()

    train_loader = DataLoader(EEG_Dataset(train_x, train_y), batch_size=config.DATA.BATCH_SIZE,
                              shuffle=True, num_workers=0, drop_last=True)
    val_loader = DataLoader(EEG_Dataset(*window_split(val_x, val_y, config)), batch_size=config.DATA.BATCH_SIZE * 4, shuffle=False,
                            num_workers=0, )

    model = IFNet(config.MODEL.IN_CHANS, config.MODEL.EMBED_DIMS, kernel_size=config.MODEL.KERNEL_SIZE, radix=config.MODEL.RADIX,
                  patch_size=config.MODEL.PATCH_SIZE, time_points=config.MODEL.TIME_POINTS, num_classes=config.MODEL.NUM_CLASSES, )
    print('\n', model)
    # from TEEGM.models.EEGNet import eegNet
    # model = eegNet(22, 768, F1=8, D=2, nClass=4, C1=63, dropoutP=0.5)
    # from TEEGM.models.FBCNet import FBCNet
    # model = FBCNet(22, 750, nClass=4, strideFactor=3)
    model.cuda()
    total_trainable_params = sum(
        p.numel() for p in model.parameters() if p.requires_grad)
    print(f'{total_trainable_params:,} training parameters.')

    criterion = nn.CrossEntropyLoss(reduction='mean')
    optimizer = build_optimizer(model, config)
    #scheduler = build_lr_scheduler(optimizer, config, len(train_loader))
    scheduler = None

    model, optimizer_state, best_train_loss, best_val_loss = train(model, train_loader, val_loader, criterion, optimizer, scheduler,
                                              config.TRAIN.EPOCHS, plot=True, rta=rta)

    train_loader = DataLoader(EEG_Dataset(*window_split(train_x, train_y, config)), batch_size=config.DATA.BATCH_SIZE * 4,
                              shuffle=False, num_workers=0, )
    best_train_loss, _ = validate(model, train_loader,)
    print(f'best train loss {best_train_loss}')
    best_val_loss, best_val_acc = validate(model, val_loader)

    if config.TRAIN.RETRAIN:
        retrainer = build_retrainer(config, train_x, train_y, val_x, val_y, model)
        retrainer['optimizer'].load_state_dict(optimizer_state)
        model = retrain(best_train_loss, model, retrainer['trainval_loader'], retrainer['val_loader'],
                        criterion, retrainer['optimizer'], retrainer['scheduler'],
                        config.TRAIN.RETRAIN_EPOCHS, rta=rta)
    return model, best_val_acc


def main(config: CfgNode):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    #print(os.getcwd())

    torch.cuda.set_device(config.DEVICE)
    seed_torch(config.SEED)
    print(f'device {config.DEVICE} is used for training')

    train_datasets = build_datasets_files(config, stage='train') # file list in subject list

    acc_subjects = []
    for i in range(len(train_datasets)):
        subject = train_datasets[i][0].split('/')[-2]
        print(f'------start {subject} training------')

        if config.EVAL:
            models = torch.load(f'{config.OUTPUT}/models/{subject}_{config.TAG}.pth', map_location=f'cuda:{torch.cuda.current_device()}')

        subject_train_files = train_datasets[i]
        subject_trainval_gen = k_fold_generator(config, subject_train_files)
        proxy_acc_subject = []  # compatible with two-stage training strategy
        val_acc_subject = []
        model_set = []
        for k, (train_x, train_y, val_x, val_y) in enumerate(subject_trainval_gen):
            print(f'------start {k + 1} fold------')
            for _ in range(config.TRAIN.REPEAT):
                torch.cuda.empty_cache()
                if config.EVAL:
                    model = models.pop(0)
                    proxy_acc_subject.append(0.)
                else:
                    pseudo_train_x, proxy_x, pseudo_train_y, proxy_y = train_test_split(train_x, train_y, test_size=1/config.DATA.K_FOLD, random_state=config.SEED)
                    model, proxy_acc = train_model(config, pseudo_train_x, pseudo_train_y, proxy_x, proxy_y, subject)
                    proxy_acc_subject.append(proxy_acc)
                    if config.SAVE:
                        model_set.append(model.cpu())
                val_loader = DataLoader(EEG_Dataset(*window_split(torch.from_numpy(val_x).cuda(), torch.from_numpy(val_y).long().cuda(), config)),
                                        batch_size=config.DATA.BATCH_SIZE * 4, shuffle=False, num_workers=0, )
                val_acc_subject.append(evaluate(model, val_loader))
        if config.SAVE and not config.EVAL:
            os.makedirs(f'{config.OUTPUT}/models/', exist_ok=True)
            torch.save(model_set, f'{config.OUTPUT}/models/{subject}_{config.TAG}.pth')
        acc_subjects.append([subject, np.mean(val_acc_subject), np.mean(proxy_acc_subject)] +
                            [*val_acc_subject, *proxy_acc_subject])
        print(f'{subject}: val set acc: {acc_subjects[-1][1]:.4f}\n')

    os.makedirs(f'{config.OUTPUT}/', exist_ok=True)
    file = f'{config.OUTPUT}/{config.EVAL_TAG}.csv' if config.EVAL else f'{config.OUTPUT}/{config.TAG}.csv'
    with open(file, 'w') as f:
        writer = csv.writer(f)
        writer.writerow(['subject', 'val_acc', 'proxy_acc', *[''] * (2 * config.DATA.K_FOLD * config.TRAIN.REPEAT)])
        for acc_subject in acc_subjects:
            writer.writerow([acc_subject[0], *[f'{acc:.4f}' for acc in acc_subject[1:]]])
        proxy_acc_avg = np.mean([acc[2] for acc in acc_subjects])
        val_acc_avg = np.mean([acc[1] for acc in acc_subjects])
        writer.writerow(['Avg', f'{val_acc_avg:.4f}', f'{proxy_acc_avg:.4f}'])

    print(f'TAG: {config.TAG}')
    print(f"Subjects' proxy set average acc: {proxy_acc_avg:.4f}")
    print(f"Subjects' val set average acc: {val_acc_avg:.4f}")


if __name__ == '__main__':
    config = get_config()
    main(config)