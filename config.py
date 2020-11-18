class Config:

    # dataset config
    dataset = "coco"
    image_dir = "./data"
    val_image_dir = None  # if validation images are stored elsewhere
    train_json_path = "./train.json"
    val_json_path = "./val.json"
    negative_sampling_rate = None  # sample images with no annotations at batch level

    # aug config
    augs = dict()
    augs["hflip"] = False  # horizontal flip, either False or a probability
    augs["vflip"] = False  # vertical flip, either False or a probability
    augs["color_jitter"] = False  # Color jitter, either False or a probability
    augs["brightness"] = False  # Brightness adjustment, either False or a probability
    augs["contrast"] = False  # Contrast adjustment, either False or a probability
    augs["shiftscalerotate"] = False  # translation, either False or a probability
    augs["gamma"] = False  # gamma correction, either False or a probability
    augs[
        "rgb_shift"
    ] = False  # rgb shift, either False or a tuple (r_shift, g_shift, b_shift, proba)
    augs["sharpness"] = False  # shaprness adjustment, either False or a probability
    augs[
        "perspective"
    ] = False  # perspective transformation, either False or a probability
    augs[
        "cutout"
    ] = False  # random cutout, either False or a tuple (proba, max_h_cutout, max_w_cutout)
    augs["gaussian_blur"] = False  # Gaussian blur, either False or a probability
    augs["superpixels"] = False  # Superpixels, either False or a probability
    augs[
        "additive_noise"
    ] = False  # Gaussian Additive Noise, either False or a probability
    augs["min_visibility"] = 0.8
    augs["min_area"] = 450

    # model config
    backbone = "resnet-50"
    pretrained = True
    weights = None
    image_size = 512

    # learning config
    num_epochs = 100
    batch_size = 8
    workers = 0
    optimizer = "adam"
    base_lr = 1e-5
    final_lr = 0
    weight_decay = 1e-6
    warmup_epochs = 0
    start_warmup = 0
    #
    # # distributed config
    # dist_mode = ["DDP"]