import logging
import random
import warnings
from io import BytesIO
from typing import Dict, List, Optional, Union

import PIL
import torch
from omegaconf import DictConfig
from PIL import ImageFile
from torch import nn
from torchvision import transforms

from .utils import construct_image_processor, image_mean_std

try:
    from torchvision.transforms import InterpolationMode

    BICUBIC = InterpolationMode.BICUBIC
except ImportError:
    BICUBIC = PIL.Image.BICUBIC

from ..constants import (
    COLUMN,
    IMAGE,
    IMAGE_BYTEARRAY,
    IMAGE_VALID_NUM,
    LABEL,
    SEMANTIC_SEGMENTATION_GT,
    SEMANTIC_SEGMENTATION_IMG,
)
from .collator import PadCollator, StackCollator

logger = logging.getLogger(__name__)
ImageFile.LOAD_TRUNCATED_IMAGES = True


class SemanticSegImageProcessor:
    """
    Prepare image data for the model specified by "prefix". For multiple models requiring image data,
    we need to create a ImageProcessor for each related model so that they will have independent input.
    """

    def __init__(
        self,
        model: nn.Module,
        img_transforms: List[str],
        gt_transforms: List[str],
        train_transforms: Optional[List[str]] = None,
        val_transforms: Optional[List[str]] = None,
        norm_type: Optional[str] = None,
        max_img_num_per_col: Optional[int] = 1,
        missing_value_strategy: Optional[str] = "skip",
        requires_column_info: bool = False,
    ):
        """
        Parameters
        ----------
        model
            The model for which this processor would be created.
        img_transforms
            A list of image transforms for image.
        gt_transforms
            A list of image transforms for ground truth image.
        train_transforms
            A list of image transforms used in training for data augmentation. Note that the transform order matters.
        val_transforms
            A list of image transforms used in validation/test/prediction. Note that the transform order matters.
        norm_type
            How to normalize an image. We now support:
            - inception
                Normalize image by IMAGENET_INCEPTION_MEAN and IMAGENET_INCEPTION_STD from timm
            - imagenet
                Normalize image by IMAGENET_DEFAULT_MEAN and IMAGENET_DEFAULT_STD from timm
            - clip
                Normalize image by mean (0.48145466, 0.4578275, 0.40821073) and
                std (0.26862954, 0.26130258, 0.27577711), used for CLIP.
        max_img_num_per_col
            The maximum number of images one sample can have.
        missing_value_strategy
            How to deal with a missing image. We now support:
            - skip
                Skip this sample
        requires_column_info
            Whether to require feature column information in dataloader.
        """

        self.img_transforms, self.gt_transforms = img_transforms, gt_transforms

        self.prefix = model.prefix
        self.missing_value_strategy = missing_value_strategy
        self.requires_column_info = requires_column_info

        self.size = model.image_size
        self.mean, self.std = image_mean_std(norm_type)
        self.normalization = transforms.Normalize(self.mean, self.std)

        self.max_img_num_per_col = max_img_num_per_col
        if max_img_num_per_col <= 0:
            logger.debug(f"max_img_num_per_col {max_img_num_per_col} is reset to 1")
            max_img_num_per_col = 1
        self.max_img_num_per_col = max_img_num_per_col
        logger.debug(f"max_img_num_per_col: {max_img_num_per_col}")

        self.img_processor = construct_image_processor(
            image_transforms=self.img_transforms, size=self.size, normalization=self.normalization
        )
        self.gt_processor = construct_image_processor(
            image_transforms=self.gt_transforms, size=self.size, normalization=None
        )
        self.train_transforms = self.get_train_transforms(train_transforms)

    @property
    def image_key(self):
        return f"{self.prefix}_{IMAGE}"

    @property
    def label_key(self):
        return f"{self.prefix}_{LABEL}"

    @property
    def image_valid_num_key(self):
        return f"{self.prefix}_{IMAGE_VALID_NUM}"

    @property
    def image_column_prefix(self):
        return f"{self.image_key}_{COLUMN}"

    def collate_fn(self, image_column_names: Optional[List] = None, per_gpu_batch_size: Optional[int] = None) -> Dict:
        """
        Collate images into a batch. Here it pads images since the image number may
        vary from sample to sample. Samples with less images will be padded zeros.
        The valid image numbers of samples will be stacked into a vector.
        This function will be used when creating Pytorch DataLoader.

        Returns
        -------
        A dictionary containing one model's collator function for image data.
        """
        fn = {}
        if self.requires_column_info:
            return NotImplementedError(
                f"requires_column_info={self.requires_column_info} not implemented for semantic segmentation tasks."
            )
        fn.update(
            {
                self.image_key: PadCollator(pad_val=0),
                self.image_valid_num_key: StackCollator(),
                self.label_key: PadCollator(pad_val=0),
            }
        )

        return fn

    def process_one_sample(
        self,
        image_features: Dict[str, Union[List[str], List[bytearray]]],
        feature_modalities: Dict[str, List[str]],
        is_training: bool,
        image_mode: Optional[str] = "RGB",
    ) -> Dict:
        """
        Read images, process them, and stack them. One sample can have multiple images,
        resulting in a tensor of (n, 3, size, size), where n <= max_img_num_per_col is the available image number.

        Parameters
        ----------
        image_features
            One sample may have multiple image columns in a pd.DataFrame and multiple images
            inside each image column.
        feature_modalities
            What modality each column belongs to.
        is_training
            Whether to process images in the training mode.
        image_mode
            A string which defines the type and depth of a pixel in the image.
            For example, RGB, RGBA, CMYK, and etc.

        Returns
        -------
        A dictionary containing one sample's images and their number.
        """
        images = []
        gts = []

        ret = {}
        annotation_column = None
        for column_name, column_modality in feature_modalities.items():
            if column_modality == SEMANTIC_SEGMENTATION_IMG:
                image_column = column_name
            if column_modality == SEMANTIC_SEGMENTATION_GT:
                annotation_column = column_name

        per_col_image_features = image_features[image_column]
        if is_training or annotation_column is not None:
            per_col_gt_features = image_features[annotation_column]

        for idx, img_feature in enumerate(per_col_image_features[: self.max_img_num_per_col]):
            try:
                with PIL.Image.open(img_feature) as img:
                    img = img.convert(image_mode)
            except Exception as e:
                continue
            if annotation_column:
                gt_feature = per_col_gt_features[idx]
                with PIL.Image.open(gt_feature) as gt:
                    gt = gt.convert("L")

            if is_training:
                if random.random() < 0.5:
                    img = self.train_transforms(img)
                    gt = self.train_transforms(gt)
                img = self.img_processor(img)
                gt = self.gt_processor(gt)
            else:
                img = self.img_processor(img)
                if annotation_column is not None:
                    gt = self.gt_processor(gt)

            images.append(img)
            if is_training or annotation_column is not None:
                gts.append(gt)

        ret.update(
            {
                self.image_key: torch.cat(images, dim=0) if len(images) != 0 else torch.tensor([]),
                self.image_valid_num_key: len(images),
                self.label_key: torch.cat(gts, dim=0) if len(gts) != 0 else torch.tensor([]),
            }
        )
        return ret

    def __call__(
        self,
        images: Dict[str, List[str]],
        feature_modalities: Dict[str, Union[int, float, list]],
        is_training: bool,
    ) -> Dict:
        """
        Obtain one sample's images and customized them for a specific model.

        Parameters
        ----------
        images
            Images of one sample.
        feature_modalities
            The modality of the feature columns.
        is_training
            Whether to process images in the training mode.

        Returns
        -------
        A dictionary containing one sample's processed images and their number.
        """
        images = {k: [v] if isinstance(v, str) else v for k, v in images.items()}

        return self.process_one_sample(images, feature_modalities, is_training)

    def get_train_transforms(self, train_transforms):
        train_trans = []
        for trans_mode in train_transforms:
            if trans_mode == "random_horizontal_flip":
                train_trans.append(transforms.RandomHorizontalFlip(1.0))
        return transforms.Compose(train_trans)
