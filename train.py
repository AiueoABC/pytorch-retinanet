import argparse
import collections
import copy
import time
import numpy as np
import os
from tqdm import tqdm
import logging
import colorama
import torch
import torch.optim as optim
from torch.cuda import amp
import torch.distributed as dist
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.data.sampler import WeightedRandomSampler
from torch.utils.data.distributed import DistributedSampler
from torchvision import transforms
from tensorboardX import SummaryWriter
from warmup_scheduler import GradualWarmupScheduler

from retinanet import model
from retinanet.dataloader import (
    CocoDataset,
    CSVDataset,
    collater,
    Resizer,
    AspectRatioBasedSampler,
    Augmenter,
    Normalizer,
)

from retinanet.augmentation import (
    RandomHorizontalFlip,
    RandomRotate,
    RandomShear,
    RandomBrightnessAdjust,
    RandomContrastAdjust,
    RandomGammaCorrection,
    RandomSaturationAdjust,
    RandomHueAdjust,
    RandomShapren,
    RandomGaussianBlur,
    RandAugment,
    get_aug_map,
)
from retinanet.utils import get_logger, AverageMeter
from retinanet.larc import LARC
from retinanet import coco_eval
from retinanet import csv_eval

assert torch.__version__.split(".")[0] == "1"

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True


def init_distributed_mode(args):
    """
    Initialize the following variables:
        - world_size
        - rank
    """
    if not dist.is_available():
        return

    args.is_slurm_job = "SLURM_JOB_ID" in os.environ

    if args.is_slurm_job:
        args.rank = int(os.environ["SLURM_PROCID"])
        args.world_size = int(os.environ["SLURM_NNODES"]) * int(
            os.environ["SLURM_TASKS_PER_NODE"][0]
        )
    else:
        # multi-GPU job (local or multi-node) - jobs started with torch.distributed.launch
        # read environment variables
        args.rank = int(os.environ["RANK"])
        args.world_size = int(os.environ["WORLD_SIZE"])

    # prepare distributed
    dist.init_process_group(
        backend="nccl", init_method=args.dist_url, world_size=args.world_size, rank=args.rank,
    )

    # set cuda device
    args.gpu_to_work_on = args.rank % torch.cuda.device_count()
    torch.cuda.set_device(args.gpu_to_work_on)
    return


parser = argparse.ArgumentParser(
    description="Simple training script for training a RetinaNet network."
)
parser.add_argument("--dataset", help="Dataset type, must be one of csv or coco.")
parser.add_argument("--train-json-path", help="Path to COCO directory")
parser.add_argument("--val-json-path", help="Path to COCO directory")
parser.add_argument("--image-dir", help="Path to the images")
parser.add_argument("--csv_train", help="Path to file containing training annotations (see readme)")
parser.add_argument("--csv_classes", help="Path to file containing class list (see readme)")
parser.add_argument(
    "--csv_val", help="Path to file containing validation annotations (optional, see readme)",
)

parser.add_argument(
    "--depth", help="Resnet depth, must be one of 18, 34, 50, 101, 152", type=int, default=50,
)
parser.add_argument("--epochs", help="Number of epochs", type=int, default=100)
parser.add_argument("--batch-size", type=int, help="batch_size", default=8)
parser.add_argument(
    "--num-workers", type=int, help="number of workers for dataloader mp", default=0
)
parser.add_argument("--logdir", type=str, help="path to save the logs and checkpoints")

parser.add_argument("--plot", action="store_true", help="whether to plot images in tensorboard")
parser.add_argument(
    "--nsr", type=float, default=None, help="whether to use negative sampling of images",
)

parser.add_argument(
    "--augs",
    help="available augs:rand,hflip,rotate,shear,brightness,contrast,hue,gamma,saturation,sharpen,gblur should be seperated by spaces.",
    nargs="+",
)
parser.add_argument(
    "--augs-prob", type=float, help="probability of applying augmentation in range [0.,1.]",
)

parser.add_argument(
    "--dist_url",
    default="env://",
    type=str,
    help="""url used to set up distributed
                    training; see https://pytorch.org/docs/stable/distributed.html""",
)
parser.add_argument(
    "--world_size",
    default=-1,
    type=int,
    help="""
                    number of processes: it is set automatically and
                    should not be passed as argument""",
)
parser.add_argument(
    "--rank",
    default=0,
    type=int,
    help="""rank of this process:
                    it is set automatically and should not be passed as argument""",
)
parser.add_argument(
    "--local_rank", default=0, type=int, help="this argument is not used and should be ignored"
)


def main():
    global args
    args = parser.parse_args()

    try:
        os.makedirs(args.logdir, exist_ok=True)
    except Exception as exc:
        raise exc

    log_file = os.path.join(args.logdir, "train.log")
    logger = get_logger(__name__, log_file)

    try:
        init_distributed_mode(args)
        distributed = True
    except KeyError:
        logger.info("Running in serial mode.")
        args.rank = 0
        distributed = False

    writer = SummaryWriter(logdir=args.logdir)

    ## print out basic info
    logger.info("CUDA available: {}".format(torch.cuda.is_available()))
    logger.info(f"torch.__version__ = {torch.__version__}")

    # Create the data loaders
    if args.dataset == "coco":

        # if args.coco_path is None:
        #     raise ValueError("Must provide --coco_path when training on COCO,")
        train_transforms = [Normalizer()]
        if args.augs is None:
            train_transforms.append(Resizer())
        else:
            p = 0.5
            if args.augs_prob is not None:
                p = args.augs_prob
            aug_map = get_aug_map(p=p)
            for aug in args.augs:
                if aug in aug_map.keys():
                    train_transforms.append(aug_map[aug])
                else:
                    logger.info(f"{aug} is not available.")
            train_transforms.append(Resizer())

        if len(train_transforms) == 2:
            logger.info(
                "Not applying any special augmentations, using only {}".format(train_transforms)
            )
        else:
            logger.info("Applying augmentations {} with probability {}".format(train_transforms, p))

        dataset_train = CocoDataset(
            args.image_dir, args.train_json_path, transform=transforms.Compose(train_transforms),
        )
        dataset_val = CocoDataset(
            args.image_dir,
            args.val_json_path,
            transform=transforms.Compose([Normalizer(), Resizer()]),
            return_ids=True,
        )

    elif args.dataset == "csv":

        if args.csv_train is None:
            raise ValueError("Must provide --csv_train when training on COCO,")

        if args.csv_classes is None:
            raise ValueError("Must provide --csv_classes when training on COCO,")

        dataset_train = CSVDataset(
            train_file=args.csv_train,
            class_list=args.csv_classes,
            transform=transforms.Compose([Normalizer(), Augmenter(), Resizer()]),
        )

        if args.csv_val is None:
            dataset_val = None
            print("No validation annotations provided.")
        else:
            dataset_val = CSVDataset(
                train_file=args.csv_val,
                class_list=args.csv_classes,
                transform=transforms.Compose([Normalizer(), Resizer()]),
            )

    else:
        raise ValueError("Dataset type not understood (must be csv or coco), exiting.")

    if dist.is_available() and distributed:
        sampler = DistributedSampler(dataset_train)
        dataloader_train = DataLoader(
            dataset_train,
            sampler=sampler,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            collate_fn=collater,
        )

    elif args.nsr is not None:
        logger.info(f"using WeightedRandomSampler with negative (image) sample rate = {args.nsr}")
        weighted_sampler = WeightedRandomSampler(
            dataset_train.weights, len(dataset_train), replacement=True
        )
        dataloader_train = DataLoader(
            dataset_train,
            num_workers=args.num_workers,
            collate_fn=collater,
            sampler=weighted_sampler,
            batch_size=args.batch_size,
            # pin_memory=True,
        )

    else:
        sampler = AspectRatioBasedSampler(
            dataset_train, batch_size=args.batch_size, drop_last=False
        )
        dataloader_train = DataLoader(
            dataset_train, num_workers=args.num_workers, collate_fn=collater, batch_sampler=sampler,
        )

    if dataset_val is not None:
        sampler_val = AspectRatioBasedSampler(
            dataset_val, batch_size=args.batch_size, drop_last=False
        )
        dataloader_val = DataLoader(
            dataset_val,
            num_workers=args.num_workers,
            collate_fn=collater,
            batch_sampler=sampler_val,
        )

    # Create the model
    if args.depth == 18:
        retinanet = model.resnet18(num_classes=dataset_train.num_classes, pretrained=True)
    elif args.depth == 34:
        retinanet = model.resnet34(num_classes=dataset_train.num_classes, pretrained=True)
    elif args.depth == 50:
        retinanet = model.resnet50(num_classes=dataset_train.num_classes, pretrained=True)
    elif args.depth == 101:
        retinanet = model.resnet101(num_classes=dataset_train.num_classes, pretrained=True)
    elif args.depth == 152:
        retinanet = model.resnet152(num_classes=dataset_train.num_classes, pretrained=True)
    else:
        raise ValueError("Unsupported model depth, must be one of 18, 34, 50, 101, 152")

    use_gpu = True

    if torch.cuda.is_available():
        if dist.is_available() and distributed:
            retinanet = nn.SyncBatchNorm.convert_sync_batchnorm(retinanet)
            retinanet = retinanet.cuda()
        else:
            torch.cuda.set_device(torch.device("cuda:0"))
            retinanet = retinanet.cuda()

    # swav = torch.load("/home/bishwarup/Desktop/swav_ckp-50.pth", map_location=torch.device("cpu"))[
    #     "state_dict"
    # ]
    # swav_dict = collections.OrderedDict()
    # for k, v in swav.items():
    #     k = k[7:]  # discard the module. part
    #     if k in retinanet.state_dict():
    #         swav_dict[k] = v
    # logger.info(f"SwAV => {len(swav_dict)} keys matched")
    # model_dict = copy.deepcopy(retinanet.state_dict())
    # model_dict.update(swav_dict)
    # retinanet.load_state_dict(model_dict)

    # if use_gpu:
    #     if torch.cuda.is_available():

    # if torch.cuda.is_available():
    #     retinanet = torch.nn.DataParallel(retinanet).cuda()
    # else:
    #     retinanet = torch.nn.DataParallel(retinanet)

    retinanet.training = True

    optimizer = optim.Adam(retinanet.parameters(), lr=1e-5)
    # optimizer = optim.SGD(retinanet.parameters(), lr=0.0001, momentum=0.95)

    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=1e-6)

    if distributed and dist.is_available():
        retinanet = nn.parallel.DistributedDataParallel(
            retinanet, device_ids=[args.gpu_to_work_on], find_unused_parameters=True
        )

    # scheduler_warmup = GradualWarmupScheduler(
    #     optimizer, multiplier=100, total_epoch=5, after_scheduler=scheduler
    # )
    # scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=3, verbose=True)
    # scheduler = optim.lr_scheduler.OneCycleLR(
    #     optimizer,
    #     max_lr=1e-4,
    #     total_steps=args.epochs * len(dataloader_train),
    #     pct_start=0.2,
    #     max_momentum=0.95,
    # )

    loss_hist = collections.deque(maxlen=500)

    if dist.is_available() and distributed:
        retinanet.module.train()
        retinanet.module.freeze_bn()
    else:
        retinanet.train()
        retinanet.freeze_bn()
    # retinanet.module.freeze_bn()

    logger.info("Num training images: {}".format(len(dataset_train)))

    # scaler = amp.GradScaler()
    best_map = 0
    n_iter = 0

    scaler = amp.GradScaler(enabled=True)

    for epoch_num in range(args.epochs):

        # scheduler_warmup.step(epoch_num)
        if dist.is_available() and distributed:
            dataloader_train.sampler.set_epoch(epoch_num)
            retinanet.module.train()
            retinanet.module.freeze_bn()
        else:
            retinanet.train()
            retinanet.freeze_bn()
        # retinanet.module.freeze_bn()

        epoch_loss = []
        pbar = tqdm(enumerate(dataloader_train), total=len(dataloader_train))
        for iter_num, data in pbar:
            try:
                optimizer.zero_grad()

                if torch.cuda.is_available():
                    with amp.autocast():
                        classification_loss, regression_loss = retinanet(
                            [data["img"].cuda().float(), data["annot"].cuda()]
                        )
                else:
                    classification_loss, regression_loss = retinanet(
                        [data["img"].float(), data["annot"]]
                    )
                n_iter = epoch_num * len(dataloader_train) + iter_num
                classification_loss = classification_loss.mean()
                regression_loss = regression_loss.mean()

                loss = classification_loss + regression_loss
                for param_group in optimizer.param_groups:
                    lr = param_group["lr"]

                if args.rank == 0:
                    writer.add_scalar("Learning rate", lr, n_iter)
                pbar_desc = f"Epoch: {epoch_num} | lr = {lr:0.6f} | batch: {iter_num} | cls: {classification_loss:.4f} | reg: {regression_loss:.4f}"
                pbar.set_description(pbar_desc)
                pbar.update(1)
                if bool(loss == 0):
                    continue

                # loss.backward()
                scaler.scale(loss).backward()

                torch.nn.utils.clip_grad_norm_(retinanet.parameters(), 0.1)

                # optimizer.step()
                # scheduler.step()  # one cycle lr operates at batch level
                scaler.step(optimizer)
                scaler.update()

                loss_hist.append(float(loss))

                epoch_loss.append(float(loss))

                del classification_loss
                del regression_loss
            except Exception as e:
                print(e)
                continue

        if args.dataset == "coco":

            # print("Evaluating dataset")
            if args.plot:
                stats = coco_eval.evaluate_coco(
                    dataset_val,
                    retinanet,
                    args.logdir,
                    args.batch_size,
                    args.num_workers,
                    writer,
                    n_iter,
                )
            else:
                stats = coco_eval.evaluate_coco(
                    dataset_val, retinanet, args.logdir, args.batch_size, args.num_workers,
                )

            map_avg, map_50, map_75, map_small = stats[:4]
            # if args.rank
            if map_50 > best_map:
                torch.save(
                    retinanet, os.path.join(args.logdir, f"retinanet_resnet50_best.pt"),
                )
                best_map = map_50
            writer.add_scalar("eval/map@0.5:0.95", map_avg, epoch_num * len(dataloader_train))
            writer.add_scalar("eval/map@0.5", map_50, epoch_num * len(dataloader_train))
            writer.add_scalar("eval/map@0.75", map_75, epoch_num * len(dataloader_train))
            writer.add_scalar("eval/map_small", map_small, epoch_num * len(dataloader_train))
            logger.info(
                f"Epoch: {epoch_num} | lr = {lr:.6f} |map@0.5:0.95 = {map_avg:.4f} | map@0.5 = {map_50:.4f} | map@0.75 = {map_75:.4f} | map-small = {map_small:.4f}"
            )

        elif args.dataset == "csv" and args.csv_val is not None:

            # logger.info("Running eval...")

            mAP = csv_eval.evaluate(dataset_val, retinanet)

        # scheduler.step(np.mean(epoch_loss))
        scheduler.step()
        # torch.save(retinanet.module, os.path.join(args.logdir, f"retinanet_{epoch_num}.pt"))

    retinanet.eval()

    # torch.save(retinanet, os.path.join(args.logdir, f"retinanet_final.pt"))


if __name__ == "__main__":
    main()
