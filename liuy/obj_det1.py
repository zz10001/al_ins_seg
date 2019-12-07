import cv2
import torch
import torch.nn as nn
import logging
import os
from detectron2.utils.visualizer import Visualizer
import dill as pickle
from collections import OrderedDict
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.engine import default_setup, DefaultPredictor
from detectron2.config.config import get_cfg
from alcloud.alcloud.model_updating.interface import BaseDeepModel
from detectron2.data import DatasetCatalog, MetadataCatalog
from detectron2.engine import default_argument_parser
from detectron2.evaluation import verify_results, SemSegEvaluator, COCOEvaluator, COCOPanopticEvaluator, \
    CityscapesEvaluator, PascalVOCDetectionEvaluator, LVISEvaluator, DatasetEvaluators
from detectron2.modeling import GeneralizedRCNNWithTTA
from detectron2.utils import comm
from liuy.reg_dataset1 import get_custom_dicts
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
TRAINED_MODEL_DIR = '/media/tangyp/Data/model_file/trained_model'


"""把setup 作为内置函数
    通过函数名来建立model 而不是让控制命令行参数args确定
    model 文件的保存路径更改 具体到某一个project
    model模型的保存
    
    fit 函数里面把一些参数定义为属性
"""



class Detctron2AlObjDetModel(BaseDeepModel):
    """Faster_RCNN"""

    def __init__(self, args, project_id, model_name=None, num_classes=None, pytorch_model=None):
        self.args = args
        self.num_class = num_classes
        self.project_id = project_id
        self.model_name = model_name
        self.num_classes =num_classes
        self.data_dir = None
        self.lr = None
        self.cfg = self.setup()
        super(Detctron2AlObjDetModel, self).__init__(project_id)
        self.model, self.device = load_prj_model(project_id=project_id)
        if self.model is None:
            if pytorch_model:
                assert isinstance(
                    pytorch_model, nn.Module), 'pytorch_model must inherit from torch.nn.Module'
                self.model = pytorch_model
                print("get a pre-trained model from parameter for project{}".format(project_id))
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
        else:
            print("load project {} model from file".format(project_id))
        print(self.model)

    def setup(self):
        """
            Create configs and perform basic setups.
            """
        cfg = get_cfg()
        cfg.merge_from_file(MODEL_NAME[self.model_name])
        cfg.merge_from_list(args.opts)
        cfg.MODEL.ROI_HEADS.NUM_CLASSES = self.num_classes
        if self.data_dir is not None:
            DatasetCatalog.register("custom", lambda data_dir=self.data_dir: get_custom_dicts(data_dir))
            cfg.DATASETS.TRAIN = ("custom",)
        if self.lr is not None:
            cfg.SOLVER.BASE_LR = self.lr
        cfg.OUTPUT_DIR = os.path.join('/media/tangyp/Data/model_file/OUTPUT_DIR', 'project_' + self.project_id)
        # cfg.freeze()
        default_setup(cfg, args)
        return cfg
    def fit(self, data_dir, label=None, transform=None,
            batch_size=1, shuffle=False, data_names=None,
            optimize_method='Adam', optimize_param=None,
            loss='CrossEntropyLoss', loss_params=None, num_epochs=10,
            save_model=True, test_label=None, **kwargs):
        self.data_dir = data_dir
        print("Command Line Args:", args)
        launch(
            self.func,
            args.num_gpus,
            num_machines=args.num_machines,
            machine_rank=args.machine_rank,
            dist_url=args.dist_url,
            args=(args, data_dir, self.model),
        )
        self.save_model()

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
                      conf_thres=0.7, nms_thres=0.4,
                      verbose=True, **kwargs):
        """
                   During inference, the model requires only the input tensors, and returns the post-processed
                   predictions as a List[Dict[Tensor]], one for each input image. The fields of the Dict are as
                   follows:
                       - boxes (Tensor[N, 4]): the predicted boxes in [x0, y0, x1, y1] format, with values between
                         0 and H and 0 and W
                       - labels (Tensor[N]): the predicted labels for each image
                       - scores (Tensor[N]): the scores or each prediction
        """
        cfg = self.setup()
        cfg.MODEL.WEIGHTS = os.path.join('/media/tangyp/Data/model_file/OUTPUT_DIR', 'model_final.pth')
        cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST =conf_thres
        predictor = DefaultPredictor(cfg)
        DatasetCatalog.register("custom_val", lambda data_dir=data_dir: get_custom_dicts(data_dir))
        data_loader = LiuyTrainer.build_test_loader(self.cfg, "custom_val")
        results = []
        for batch in data_loader:
            for item in batch:
                file_name = item['file_name']
                img = cv2.imread(file_name)
                prediction = predictor(img)
                record = {'boxes': prediction['instances'].pred_boxes, 'labels': prediction['instances'].pred_classes, \
                          'scores': prediction['instances'].scores}
                results.append(record)
                # visualizer = Visualizer(img[:, :, ::-1],
                #                         metadata=MetadataCatalog.get(
                #                             self.cfg.DATASETS.TEST[0] if len(self.cfg.DATASETS.TEST) else "__unused"
                #                         ),
                #                         scale=0.8, instance_mode=1
                #                         )
                # instances = prediction["instances"].to('cpu')
                # vis_output = visualizer.draw_instance_predictions(predictions=instances)
                # save_path = os.path.join('/media/tangyp/Data/model_file/output_test', os.path.basename(file_name))
                # vis_output.save(save_path)
        return results

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
        with open(os.path.join(TRAINED_MODEL_DIR, self._proj_id + '_model.pkl'), 'wb') as f:
            pickle.dump(self.trainer.model, f)






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
    cfg.OUTPUT_DIR = '/media/tangyp/Data/model_file/OUTPUT_DIR'
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
    data_val_dir = '/media/tangyp/Data/coco/annotations/instances_val2014.json'
    args = default_argument_parser().parse_args()
    model = Detctron2AlObjDetModel(args=args, project_id='1', model_name='Faster_RCNN', num_classes=80)
    model.fit(data_dir)
    # proba = model.predict_proba(data_dir=data_val_dir)
    debug = 1