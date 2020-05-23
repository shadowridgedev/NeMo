# =============================================================================
# Copyright 2020 NVIDIA. All Rights Reserved.
# Copyright 2019 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================

'''
This file contains code artifacts adapted from the original implementation:
https://github.com/google-research/google-research/blob/master/schema_guided_dst/baseline/train_and_predict.py
'''

import torch

from nemo import logging
from nemo.backends.pytorch import LossNM
from nemo.collections.nlp.data.datasets.sgd_dataset.input_example import IGNORE_INDEX, STATUS_ACTIVE
from nemo.core import ChannelType, LabelsType, LogitsType, NeuralType
from nemo.utils.decorators import add_port_docs

__all__ = ['SGDDialogueStateLossNM']


class SGDDialogueStateLossNM(LossNM):
    """
    Neural module which implements loss for SGD model.
    """

    @property
    @add_port_docs
    def input_ports(self):
        """Returns definitions of module input ports.
            logit_intent_status (float): Output of SGD model
            intent_status_labels (int): Intent labels
            logit_req_slot_status (float): Output of SGD model
            requested_slot_status (float): Takes value 1 if the corresponding slot is requested, 0 otherwise
            req_slot_mask (bool): Masks requested slots not used for the particular service
            logit_cat_slot_status (float): Output of SGD model
            categorical_slot_status (int): The status of each categorical slot in the service
            logit_cat_slot_value (float): Output of SGD model
            categorical_slot_values (int): The index of the correct value for each categorical slot
            logit_noncat_slot_status (float): Output of SGD model
            noncategorical_slot_status (int): The status of each noncategorical slot in the service
            logit_noncat_slot_start (float): Output of SGD model
            logit_noncat_slot_end (float): Output of SGD model
            noncategorical_slot_value_start (int): The index of the starting subword corresponding to the slot span for a non-categorical slot value
            noncategorical_slot_value_end (int): The index of the ending (inclusive) subword corresponding to the slot span for a non-categorical slot value
        """
        return {
            "logit_intent_status": NeuralType(('B', 'T', 'C'), LogitsType()),
            "intent_status_labels": NeuralType(('B'), LabelsType()),
            "logit_req_slot_status": NeuralType(('B', 'T'), LogitsType()),
            "requested_slot_status": NeuralType(('B', 'T'), LabelsType()),
            "req_slot_mask": NeuralType(('B', 'T'), ChannelType()),
            "logit_cat_slot_status": NeuralType(('B', 'T', 'C'), LogitsType()),
            "categorical_slot_status": NeuralType(('B', 'T'), LabelsType()),
            "logit_cat_slot_value": NeuralType(('B', 'T', 'C'), LogitsType()),
            "categorical_slot_values": NeuralType(('B', 'T'), LabelsType()),
            "logit_noncat_slot_status": NeuralType(('B', 'T', 'C'), LogitsType()),
            "noncategorical_slot_status": NeuralType(('B', 'T'), LabelsType()),
            "logit_noncat_slot_start": NeuralType(('B', 'T', 'C'), LogitsType()),
            "logit_noncat_slot_end": NeuralType(('B', 'T', 'C'), LogitsType()),
            "noncategorical_slot_value_start": NeuralType(('B', 'T'), LabelsType()),
            "noncategorical_slot_value_end": NeuralType(('B', 'T'), LabelsType()),
        }

    @property
    def output_ports(self):
        """
        Returns definitions of module output ports.
        loss:
            NeuralType(None)
        """
        return {"loss": NeuralType(None)}

    def __init__(self, reduction='mean'):
        """
        Args:
            reduction (str): specifies the reduction to apply to the final loss, choose 'mean' or 'sum'
        """
        super().__init__()

        if reduction not in ['mean', 'sum']:
            logging.warning(f'{reduction} reduction is not supported. Setting reduction to "mean"')
            reduction = 'mean'

        self.reduction = reduction
        self._cross_entropy = torch.nn.CrossEntropyLoss(reduction=self.reduction, ignore_index=IGNORE_INDEX)
        self._criterion_req_slots = torch.nn.BCEWithLogitsLoss(reduction=self.reduction)

    def _loss_function(
        self,
        logit_intent_status,
        intent_status_labels,
        logit_req_slot_status,
        requested_slot_status,
        req_slot_mask,
        logit_cat_slot_status,
        categorical_slot_status,
        logit_cat_slot_value,
        categorical_slot_values,
        logit_noncat_slot_status,
        noncategorical_slot_status,
        logit_noncat_slot_start,
        logit_noncat_slot_end,
        noncategorical_slot_value_start,
        noncategorical_slot_value_end,
    ):
        # Intent loss
        intent_loss = self._cross_entropy(logit_intent_status, intent_status_labels)

        # Requested slots.
        # Shape: (batch_size, max_num_slots)
        # mask unused slots
        # Sigmoid cross entropy is used because more than one slots can be requested in a single utterance

        requested_slot_loss = self._criterion_req_slots(
            logit_req_slot_status.view(-1)[req_slot_mask], requested_slot_status.view(-1)[req_slot_mask]
        )

        # Categorical slot status
        # Shape of logit_cat_slot_status: (batch_size, max_num_cat_slots, 3)
        categorical_slot_status = categorical_slot_status.view(-1)
        if sum(categorical_slot_status != IGNORE_INDEX) == 0:
            logging.warning(f'No active categorical slots in the batch')
            cat_slot_status_loss = 0 * torch.sum(logit_cat_slot_status.view(-1))
        else:
            cat_slot_status_loss = self._cross_entropy(logit_cat_slot_status.view(-1, 3), categorical_slot_status)

        # Categorical slot values.
        # Shape: (batch_size, max_num_cat_slots, max_num_slot_values).
        max_num_slot_values = logit_cat_slot_value.size()[-1]

        # Zero out losses for categorical slot value when the slot status is not active.
        categorical_slot_values = categorical_slot_values.view(-1)
        # to handle cases with no active categorical slot value
        if sum(categorical_slot_values.view(-1) != IGNORE_INDEX) == 0:
            logging.warning(f'No active values for categorical slots in the batch.')
            cat_slot_value_loss = 0 * torch.sum(logit_cat_slot_value.view(-1))
        else:
            cat_slot_value_loss = self._cross_entropy(
                logit_cat_slot_value.view(-1, max_num_slot_values), categorical_slot_values
            )

        # Non-categorical slot status.
        # Shape: (batch_size, max_num_noncat_slots, 3).
        noncategorical_slot_status = noncategorical_slot_status.view(-1)
        if sum(noncategorical_slot_status != IGNORE_INDEX) == 0:
            logging.warning(f'No active non-categorical slots in the batch.')
            noncat_slot_status_loss = 0 * torch.sum(logit_noncat_slot_status.view(-1))
        else:
            noncat_slot_status_loss = self._cross_entropy(
                logit_noncat_slot_status.view(-1, 3), noncategorical_slot_status,
            )

        # Non-categorical slot spans.
        # Shape: (batch_size, max_num_noncat_slots, max_num_tokens).n
        max_num_tokens = logit_noncat_slot_start.size()[-1]
        # Zero out losses for non-categorical slot spans when the slot status is not active.
        noncategorical_slot_value_start = noncategorical_slot_value_start.view(-1)
        noncategorical_slot_value_end = noncategorical_slot_value_end.view(-1)
        # to handle cases with no active categorical slot value
        if sum(noncategorical_slot_value_start != IGNORE_INDEX) == 0:
            logging.warning(f'No active values for non-categorical slots in the batch.')
            span_start_loss = 0 * torch.sum(logit_noncat_slot_start.view(-1))
            span_end_loss = 0 * torch.sum(logit_noncat_slot_end.view(-1))
        else:
            span_start_loss = self._cross_entropy(
                logit_noncat_slot_start.view(-1, max_num_tokens), noncategorical_slot_value_start
            )
            span_end_loss = self._cross_entropy(
                logit_noncat_slot_end.view(-1, max_num_tokens), noncategorical_slot_value_end
            )

        losses = {
            "intent_loss": intent_loss,
            "requested_slot_loss": requested_slot_loss,
            "cat_slot_status_loss": cat_slot_status_loss,
            "cat_slot_value_loss": cat_slot_value_loss,
            "noncat_slot_status_loss": noncat_slot_status_loss,
            "span_start_loss": span_start_loss,
            "span_end_loss": span_end_loss,
        }

        total_loss = sum(losses.values())
        if self.reduction == 'mean':
            total_loss = total_loss / len(losses)
        else:
            batch_size = logit_intent_status.shape[0]
            total_loss = total_loss / batch_size
        return total_loss
