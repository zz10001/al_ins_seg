import torch
import torch.nn as nn
import logging
import os
from collections import OrderedDict
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.engine import default_setup
from detectron2.config.config import get_cfg
from alcloud.alcloud.model_updating.interface import BaseDeepModel
from detectron2.data import DatasetCatalog, MetadataCatalog
from detectron2.engine import default_argument_parser
from detectron2.evaluation import verify_results, SemSegEvaluator, COCOEvaluator, COCOPanopticEvaluator, \
    CityscapesEvaluator, PascalVOCDetectionEvaluator, LVISEvaluator, DatasetEvaluators
from detectron2.modeling import GeneralizedRCNNWithTTA
from detectron2.utils import comm
from liuy.reg_dataset import get_custom_dicts
from detectron2.engine.defaults import DefaultTrainer
from detectron2.engine import hooks
from alcloud.alcloud.utils.data_manipulate import create_img_dataloader, create_faster_rcnn_dataloader
from alcloud.alcloud.utils.detection.engine import evaluate
from alcloud.alcloud.utils.torch_utils import load_prj_model
from liuy.LiuyTrainer import  LiuyTrainer
from detectron2.engine import launch
MODEL_NAME = {'Faster_RCNN': '/home/tangyp/detectron2/configs/COCO-Detection/faster_rcnn_R_50_C4_1x.yaml',
              }

__all__ = ['Detctron2AlObjDetModel',
           ]

"""
Pytorch official faster rcnn dataset requirement:

label_dict: dict
    key: file name
    value: list [[label_idx x0 y0 x1 y1], ...]

the dataset __getitem__ of object detection should return:

    image: a PIL Image of size (H, W)
    target: a dict containing the following fields
        boxes (FloatTensor[N, 4]): the coordinates of the N bounding boxes in [x0, y0, x1, y1] format, ranging from 0 to W and 0 to H
        labels (Int64Tensor[N]): the label for each bounding box
        image_id (Int64Tensor[1]): an image identifier. It should be unique between all the images in the dataset, and is used during evaluation
        area (Tensor[N]): The area of the bounding box. This is used during evaluation with the COCO metric, to separate the metric scores between small, medium and large boxes.
        iscrowd (UInt8Tensor[N]): instances with iscrowd=True will be ignored during evaluation.
        (optionally) masks (UInt8Tensor[N, H, W]): The segmentation masks for each one of the objects
        (optionally) keypoints (FloatTensor[N, K, 3]): For each one of the N objects, it contains the K keypoints in [x, y, visibility] format, defining the object. visibility=0 means that the keypoint is not visible. Note that for data augmentation, the notion of flipping a keypoint is dependent on the data representation, and you should probably adapt references/detection/transforms.py for your new keypoint representation


YOLOv3 dataset requirements:

ImgObjDetDataset(data_dir, label_dict)
label_dict: dict
    key: file name
    value: list [[label_idx x_center y_center width height], ...]
"""


class Detctron2AlObjDetModel(BaseDeepModel):
    """Faster_RCNN"""

    def __init__(self, args, project_id, model_name=None, num_classes=None, pytorch_model=None):
        self.args = args
        self.num_class = num_classes
        self.cfg = setup(args,  num_classes=num_classes)
        super(Detctron2AlObjDetModel, self).__init__(project_id)
        self.model, self.device = load_prj_model(project_id=project_id)
        if self.model is None:
            if pytorch_model:
                assert isinstance(
                    pytorch_model, nn.Module), 'pytorch_model must inherit from torch.nn.Module'
                self.model = pytorch_model
            else:
                assert model_name in MODEL_NAME.keys(
                ), 'model_name must be one of {}'.format(MODEL_NAME.keys())
                if not num_classes:
                    raise ValueError(
                        "Deep model of project {} is not initialized, please specify the model name and number of classes.".format(
                            project_id))
                self.model = LiuyTrainer.build_model(self.cfg)
                self.model = self.model.to(self.device)
                print("Initialize a pre-trained model for project{}".format(project_id))
                # print(self.model)
        else:
            print("load project {} model from file".format(project_id))
        print(self.model)

    def fit(self, data_dir, label=None, transform=None,
            batch_size=1, shuffle=False, data_names=None,
            optimize_method='Adam', optimize_param=None,
            loss='CrossEntropyLoss', loss_params=None, num_epochs=10,
            save_model=True, test_label=None, **kwargs):

        print("Command Line Args:", args)
        launch(
            self.func,
            args.num_gpus,
            num_machines=args.num_machines,
            machine_rank=args.machine_rank,
            dist_url=args.dist_url,
            args=(args, data_dir, self.model),
        )

    def func(self, args, data_dir=None, model=None):
        cfg = setup(args, data_dir=data_dir)
        if args.eval_only:
            model = Trainer.build_model(cfg)
            DetectionCheckpointer(model, save_dir=cfg.OUTPUT_DIR).resume_or_load(
                cfg.MODEL.WEIGHTS, resume=args.resume
            )
            res = Trainer.test(cfg, model)
            if comm.is_main_process():
                verify_results(cfg, res)
            if cfg.TEST.AUG.ENABLED:
                res.update(Trainer.test_with_TTA(cfg, model))
            return res

        """
        If you'd like to do anything fancier than the standard training logic,
        consider writing your own training loop or subclassing the trainer.
        """
        trainer = LiuyTrainer(cfg, model)
        trainer.resume_or_load(resume=args.resume)
        if cfg.TEST.AUG.ENABLED:
            trainer.register_hooks(
                [hooks.EvalHook(0, lambda: trainer.test_with_TTA(cfg, trainer.model))]
            )
        trainer.train()

    def predict_proba(self, data_dir, data_names=None, transform=None, batch_size=1,
                      conf_thres=0.5, nms_thres=0.4,
                      verbose=True, **kwargs):
        '''proba predict.

        :param data_dir: str
            The path to the data folder.

        :param data_names: list, optional (default=None)
            The data names. If not specified, it will all the files in the
            data_dir.

        :param transform: torchvision.transforms.Compose, optional (default=None)
            Transforms object that will be applied to the image data.

        :return: pred: 2D array
            The proba prediction result. Shape [n_samples, n_classes]
        '''
        result = []
        self.model_ft.eval()
        count = 1
        dataloader = create_img_dataloader(data_dir=data_dir, labels=None, transform=transform,
                                           batch_size=1, shuffle=False, data_names=data_names)
        for batch in dataloader:
            inputs = batch['image'][0]
            # faster rcnn
            """
            During inference, the model requires only the input tensors, and returns the post-processed
            predictions as a List[Dict[Tensor]], one for each input image. The fields of the Dict are as
            follows:
                - boxes (Tensor[N, 4]): the predicted boxes in [x0, y0, x1, y1] format, with values between
                  0 and H and 0 and W
                - labels (Tensor[N]): the predicted labels for each image
                - scores (Tensor[N]): the scores or each prediction
            """
            with torch.no_grad():
                prediction = self.model_ft([inputs.to(self.device)])
                if verbose:
                    print("Prediction: " + str(count) + '/' + str(len(dataloader)))
                    count += 1
                    print(prediction)
                result.append(prediction)
        return result

    def predict(self, data_dir, data_names=None, transform=None):
        '''predict

        :param data_dir: str
            The path to the data folder.

        :param data_names: list, optional (default=None)
            The data names. If not specified, it will all the files in the
            data_dir.

        :param transform: torchvision.transforms.Compose, optional (default=None)
            Transforms object that will be applied to the image data.

        :return: pred: 1D array
            The prediction result. Shape [n_samples]
        '''
        proba_result = self.predict_proba(
            data_dir=data_dir, data_names=data_names, transform=transform)
        return proba_result

    def test(self, data_dir, label, batch_size, **kwargs):
        self.model_ft.eval()
        assert isinstance(label, dict)
        dataloader = create_faster_rcnn_dataloader(data_dir=data_dir, label_dict=label,
                                                   augment=False, batch_size=batch_size, shuffle=False)
        with torch.no_grad():
            return evaluate(self.model_ft, dataloader, self.device)

    def save_model(self):
        pass




def set_model(model_name, num_classes ):
    """
        Create configs for building model
    """
    cfg = get_cfg()
    cfg.merge_from_file(MODEL_NAME[model_name])
    cfg.MODEL.ROI_HEADS.NUM_CLASSES = num_classes
    return cfg

def set_trainer(cfg,data_dir,lr=0.00025,label=None, transform=None,
            batch_size=1, shuffle=False, data_names=None,
            optimize_method='Adam', optimize_param=None,
            loss='CrossEntropyLoss', loss_params=None, num_epochs=10,
            save_model=True, test_label=None, **kwargs):
    """
        Create configs for building trainer
    """
    # DatasetCatalog.register("custom", lambda data_dir=data_dir: get_custom_dicts(data_dir))
    # cfg.DATASETS.TRAIN = ("custom",)
    # cfg.SOLVER.BASE_LR = lr
    # cfg.freeze()
    # default_setup(cfg)
    new_cfg = cfg
    DatasetCatalog.register("custom", lambda data_dir=data_dir: get_custom_dicts(data_dir))
    new_cfg.DATASETS.TRAIN = ("custom",)
    new_cfg.SOLVER.BASE_LR = lr
    default_setup(cfg)
    return new_cfg

def setup(args, num_classes=80, lr=0.00025,data_dir = None):
    """
    Create configs and perform basic setups.
    """
    cfg = get_cfg()
    if data_dir is not None:
        DatasetCatalog.register("custom", lambda data_dir=data_dir: get_custom_dicts(data_dir))
        cfg.DATASETS.TRAIN = ("custom",)
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    cfg.SOLVER.BASE_LR = lr
    cfg.MODEL.ROI_HEADS.NUM_CLASSES = num_classes
    cfg.freeze()
    default_setup(cfg, args)
    debug = 1
    return cfg
class Trainer(DefaultTrainer):
    """
    We use the "DefaultTrainer" which contains a number pre-defined logic for
    standard training workflow. They may not work for you, especially if you
    are working on a new research project. In that case you can use the cleaner
    "SimpleTrainer", or write your own training loop.
    """

    @classmethod
    def build_evaluator(cls, cfg, dataset_name, output_folder=None):
        """
        Create evaluator(s) for a given dataset.
        This uses the special metadata "evaluator_type" associated with each builtin dataset.
        For your own dataset, you can simply create an evaluator manually in your
        script and do not have to worry about the hacky if-else logic here.
        """
        if output_folder is None:
            output_folder = os.path.join(cfg.OUTPUT_DIR, "inference")
        evaluator_list = []
        evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type
        if evaluator_type in ["sem_seg", "coco_panoptic_seg"]:
            evaluator_list.append(
                SemSegEvaluator(
                    dataset_name,
                    distributed=True,
                    num_classes=cfg.MODEL.SEM_SEG_HEAD.NUM_CLASSES,
                    ignore_label=cfg.MODEL.SEM_SEG_HEAD.IGNORE_VALUE,
                    output_dir=output_folder,
                )
            )
        if evaluator_type in ["coco", "coco_panoptic_seg"]:
            evaluator_list.append(COCOEvaluator(dataset_name, cfg, True, output_folder))
        if evaluator_type == "coco_panoptic_seg":
            evaluator_list.append(COCOPanopticEvaluator(dataset_name, output_folder))
        if evaluator_type == "cityscapes":
            assert (
                torch.cuda.device_count() >= comm.get_rank()
            ), "CityscapesEvaluator currently do not work with multiple machines."
            return CityscapesEvaluator(dataset_name)
        if evaluator_type == "pascal_voc":
            return PascalVOCDetectionEvaluator(dataset_name)
        if evaluator_type == "lvis":
            return LVISEvaluator(dataset_name, cfg, True, output_folder)
        if len(evaluator_list) == 0:
            raise NotImplementedError(
                "no Evaluator for the dataset {} with the type {}".format(
                    dataset_name, evaluator_type
                )
            )
        if len(evaluator_list) == 1:
            return evaluator_list[0]
        return DatasetEvaluators(evaluator_list)

    @classmethod
    def test_with_TTA(cls, cfg, model):
        logger = logging.getLogger("detectron2.trainer")
        # In the end of training, run an evaluation with TTA
        # Only support some R-CNN models.
        logger.info("Running inference with test-time augmentation ...")
        model = GeneralizedRCNNWithTTA(cfg, model)
        evaluators = [
            cls.build_evaluator(
                cfg, name, output_folder=os.path.join(cfg.OUTPUT_DIR, "inference_TTA")
            )
            for name in cfg.DATASETS.TEST
        ]
        res = cls.test(cfg, model, evaluators)
        res = OrderedDict({k + "_TTA": v for k, v in res.items()})
        return res






if __name__ == "__main__":
    data_dir = '/media/tangyp/Data/coco/annotations/instances_train2014.json'
    args = default_argument_parser().parse_args()
    model = Detctron2AlObjDetModel(args=args, project_id='1', model_name='Faster_RCNN', num_classes=80)
    model.fit(data_dir)