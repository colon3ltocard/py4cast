"""
This module contains the loss functions used in the training of the models.
We use Python Mixins to avoid code duplication. See https://serge-m.github.io/posts/mixins-in-python/
Our losses inherit from PyTorch's losses and are further customized by stacking Mixins,
each Mixin injects one or more method/functionnality.
In order to force the user to implement the prepare method,
which is expected by the rest of the system, we use the Py4castLoss abstract class.
"""

from abc import ABC, abstractmethod

import torch
from torch.nn import L1Loss, MSELoss

from py4cast.datasets.base import DatasetInfo, NamedTensor


class WeightedLossMixin:
    """
    Compute a weighted loss function with a weight for each feature.
    During the forward step, the loss is computed for each feature and then weighted
    and optionally averaged over the spatial dimensions.
    """

    def forward(
        self,
        prediction: NamedTensor,
        target: NamedTensor,
        reduce_spatial_dim=True,
    ) -> torch.Tensor:
        """
        Computed weighted loss function.
        prediction/target: (B, pred_steps, N_grid, d_f) or (B, pred_steps, W, H, d_f)
        returns (B, pred_steps)
        """

        # Compute Torch loss (defined in the parent class when this Mixin is used)
        torch_loss = super().forward(prediction.tensor, target.tensor)

        # Retrieve the weights for each feature
        weights = torch.stack(
            [self.loss_state_weight[name] for name in prediction.feature_names]
        ).to(torch_loss, non_blocking=True)

        # Apply the weights and sum over the feature dimension
        weighted_loss = torch.sum(torch_loss * weights, dim=-1)

        # if no reduction on spatial dimension is required, return the weighted loss
        if not reduce_spatial_dim:
            return weighted_loss

        # Compute the mean loss over all spatial dimensions
        # Take (unweighted) mean over only non-border (interior) grid nodes/pixels
        # We use forward indexing for the spatial_dim_idx of the target tensor
        # so the code below works even if the feature dimension has been reduced
        # The final shape is (B, pred_steps)

        time_step_mean_loss = (
            torch.sum(weighted_loss * self.interior_mask, dim=target.spatial_dim_idx)
            / self.num_interior
        )

        return time_step_mean_loss


class RegisterBuffersMixin:
    """
    This Mixin is used to register the interior mask and the loss_state_weight buffer
    with the lightning module.
    See https://lightning.ai/docs/pytorch/stable/accelerators/accelerator_prepare.html
    """

    def register_loss_state_buffers(
        self,
        interior_mask: torch.Tensor,
        loss_state_weight: dict,
        squeeze_mask: bool = False,
    ) -> None:
        """
        We register the state_weight buffer to the lightning module
        and keep references to other buffers of interest
        """

        self.loss_state_weight = loss_state_weight
        self.register_buffer(
            "interior_mask",
            interior_mask.squeeze(-1) if squeeze_mask else interior_mask,
        )
        self.num_interior = torch.sum(interior_mask).item()


class ScaledLossMixin:
    def forward(self, prediction: NamedTensor, target: NamedTensor) -> torch.Tensor:
        """
        Computed weighted loss function averaged over all spatial dimensions.
        prediction/target: (B, pred_steps, N_grid, d_f) or (B, pred_steps, W, H, d_f)
        returns (B, pred_steps)
        """
        # Compute Torch loss (defined in the parent class when this Mixin is used)
        torch_loss = super().forward(prediction.tensor, target.tensor)

        # Retrieve the weights
        weights = torch.stack(
            [self.loss_state_weight[name] for name in prediction.feature_names]
        ).to(torch_loss, non_blocking=True)

        # Apply weights
        torch_loss = torch_loss * weights

        # Compute the mean loss value over spatial dimensions
        mean_loss = (
            torch.sum(
                torch_loss * self.interior_mask.to(torch_loss, non_blocking=True),
                dim=target.spatial_dim_idx,
            )
            / self.num_interior
        )
        return mean_loss


class Py4CastLoss(ABC):
    """
    Abstract class to force the user to implement the prepare method because it is expected by the rest of the system.
    """

    @abstractmethod
    def prepare(self, interior_mask: torch.Tensor, dataset_info: DatasetInfo) -> None:
        """
        Prepare the loss function using the statics from the dataset
        """


class ScaledRMSELoss(RegisterBuffersMixin, MSELoss, Py4CastLoss):
    def prepare(self, interior_mask: torch.Tensor, dataset_info: DatasetInfo) -> None:
        # build the dictionnary of weight
        loss_state_weight = {}
        for name in dataset_info.state_weights:
            loss_state_weight[name] = dataset_info.stats[name]["std"]
        super().register_loss_state_buffers(interior_mask, loss_state_weight)

    def forward(self, prediction: NamedTensor, target: NamedTensor):
        """
        Computed weighted loss function.
        prediction/target: (B, pred_steps, N_grid, d_f)
        returns (B, pred_steps)
        """
        torch_mse_loss = super().forward(prediction.tensor, target.tensor)

        # Apply the interior mask - set loss to zero for border pixels/grid_nodes
        torch_mse_loss = torch_mse_loss * self.interior_mask.to(
            torch_mse_loss, non_blocking=True
        )

        # Compute the mean loss value over spatial dimensions
        mean_mse_loss = (
            torch.sum(torch_mse_loss, dim=target.spatial_dim_idx) / self.num_interior
        )

        # Retrieve the weights, one per feature
        weights = torch.stack(
            [self.loss_state_weight[name] for name in prediction.feature_names]
        ).to(torch_mse_loss, non_blocking=True)

        # Apply the weights to the square-root of the loss hence the ScaledRMSE
        return torch.sqrt(mean_mse_loss) * weights


class ScaledL1Loss(RegisterBuffersMixin, ScaledLossMixin, L1Loss, Py4CastLoss):
    """
    Computes a scaled L1 loss function with a weight for each feature.
    """

    def prepare(self, interior_mask: torch.Tensor, dataset_info: DatasetInfo) -> None:
        # build the dictionnary of weight
        loss_state_weight = {}
        for name in dataset_info.state_weights:
            loss_state_weight[name] = dataset_info.stats[name]["std"]
        super().register_loss_state_buffers(interior_mask, loss_state_weight)


class WeightedMSELoss(RegisterBuffersMixin, WeightedLossMixin, MSELoss, Py4CastLoss):
    def prepare(self, interior_mask: torch.Tensor, dataset_info: DatasetInfo) -> None:
        # build the dictionnary of weight
        loss_state_weight = {}
        for name in dataset_info.state_weights:
            loss_state_weight[name] = dataset_info.state_weights[name] / (
                dataset_info.diff_stats[name]["std"] ** 2.0
            )
        super().register_loss_state_buffers(
            interior_mask, loss_state_weight, squeeze_mask=True
        )


class WeightedL1Loss(RegisterBuffersMixin, WeightedLossMixin, L1Loss, Py4CastLoss):
    def prepare(self, interior_mask: torch.Tensor, dataset_info: DatasetInfo) -> None:
        # build the dictionnary of weight
        loss_state_weight = {}
        for name in dataset_info.state_weights:
            loss_state_weight[name] = (
                dataset_info.state_weights[name] / dataset_info.diff_stats[name]["std"]
            )
        super().register_loss_state_buffers(
            interior_mask, loss_state_weight, squeeze_mask=True
        )
