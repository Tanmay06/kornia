import logging
from typing import Callable, Dict

import torch
import torch.nn as nn
from torch.utils.data import DataLoader

# the accelerator library is a requirement for the Trainer
# but it is optional for grousnd base user of kornia.
try:
    from accelerate import Accelerator
except ImportError:
    Accelerator = None

from kornia.metrics import AverageMeter

from .utils import Configuration, TrainerState

callbacks_whitelist = [
    "preprocess", "augmentations", "evaluate", "fit", "checkpoint", "terminate"
]


class Trainer:
    """Base class to train the different models in kornia.

    .. warning::
        The API is experimental and subject to be modified based on the needs of kornia models.

    Args:
        model: the nn.Module to be optimized.
        train_dataloader: the data loader used in the training loop.
        valid_dataloader: the data loader used in the validation loop.
        criterion: the nn.Module with the function that computes the loss.
        optimizer: the torch optimizer object to be used during the optimization.
        scheduler: the torch scheduler object with defiing the scheduling strategy.
        accelerator: the Accelerator object to distribute the training.
        config: a TrainerConfiguration structure containing the experiment hyper parameters.
        callbacks: a dictionary containing the pointers to the functions to overrides. The
          main supported hooks are ``evaluate``, ``preprocess``, ``augmentations`` and ``fit``.

    .. important::
        The API heavily relies on `accelerate <https://github.com/huggingface/accelerate/>`_.
        In order to use it, you must: ``pip install kornia[x]``

    .. seealso::
        Learn how to use the API in our documentation
        `here <https://kornia.readthedocs.io/en/latest/get-started/training.html>`_.
    """
    def __init__(
        self,
        model: nn.Module,
        train_dataloader: DataLoader,
        valid_dataloader: DataLoader,
        criterion: nn.Module,
        optimizer: torch.optim.Optimizer,
        scheduler: torch.optim.lr_scheduler.CosineAnnealingLR,
        config: Configuration,
        callbacks: Dict[str, Callable] = {},
    ) -> None:
        # setup the accelerator
        if Accelerator is None:
            raise ModuleNotFoundError(
                "accelerate library is not installed: pip install kornia[x]")
        self.accelerator = Accelerator()

        # setup the data related objects
        self.model = self.accelerator.prepare(model)
        self.train_dataloader = self.accelerator.prepare(train_dataloader)
        self.valid_dataloader = self.accelerator.prepare(valid_dataloader)
        self.criterion = criterion.to(self.device)
        self.optimizer = self.accelerator.prepare(optimizer)
        self.scheduler = scheduler
        self.config = config

        # configure callbacks
        for fn_name, fn in callbacks.items():
            if fn_name not in callbacks_whitelist:
                raise ValueError(f"Not supported: {fn_name}.")
            setattr(self, fn_name, fn)

        # hyper-params
        self.num_epochs = config.num_epochs

        self._logger = logging.getLogger('train')

    @property
    def device(self) -> torch.device:
        return self.accelerator.device

    def backward(self, loss: torch.Tensor) -> None:
        self.accelerator.backward(loss)

    def fit_epoch(self, epoch: int) -> None:
        # train loop
        self.model.train()
        losses = AverageMeter()
        for sample_id, sample in enumerate(self.train_dataloader):
            source, target = sample  # this might change with new pytorch dataset structure
            self.optimizer.zero_grad()

            # perform the preprocess and augmentations in batch
            img = self.preprocess(source)
            img = self.augmentations(img)
            # make the actual inference
            output = self.model(img)
            loss = self.criterion(output, target)
            self.backward(loss)
            self.optimizer.step()

            losses.update(loss.item(), img.shape[0])

            if sample_id % 50 == 0:
                self._logger.info(
                    f"Train: {epoch + 1}/{self.num_epochs}  "
                    f"Sample: {sample_id + 1}/{len(self.train_dataloader)} "
                    f"Loss: {losses.val:.3f} {losses.avg:.3f}"
                )

    def fit(self,) -> None:
        # execute the main loop
        # NOTE: Do not change and keep this structure clear for readability.
        for epoch in range(self.num_epochs):
            # call internally the training loop
            # NOTE: override to customize your evaluation routine
            self.fit_epoch(epoch)

            # call internally the evaluation loop
            # NOTE: override to customize your evaluation routine
            valid_stats = self.evaluate()

            self.checkpoint(self.model, epoch, valid_stats)

            state = self.terminate(self.model, epoch, valid_stats)
            if state == TrainerState.TERMINATE:
                break

            # END OF THE EPOCH
            self.scheduler.step()

        ...

    def evaluate(self):
        ...

    def preprocess(self, x):
        return x

    def augmentations(self, x):
        return x

    def checkpoint(self, *args, **kwargs):
        ...

    def terminate(self, *args, **kwargs):
        ...