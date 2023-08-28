# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
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
#
# Copyright 2018-2019, Mingkun Huang
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import torch
from torch.autograd import Function
from torch.nn import Module

# from nemo.collections.asr.parts.numba.rnnt_loss import rnnt
from nemo.collections.asr.parts.numba.rnnt_loss import rnnt_dep

from nemo.collections.asr.parts.numba.rnnt_loss.utils.cpu_utils import cpu_rnnt

__all__ = ['rnnt_loss', 'RNNTLossNumba_dep', 'MultiblankRNNTLossNumba_dep', 'TDTLossNumba_dep']


class _RNNTNumba_dep(Function):
    @staticmethod
    def forward(ctx, acts, labels, act_lens, label_lens, blank, reduction, fastemit_lambda, clamp):
        """
        log_probs: Tensor of (batch x seqLength x labelLength x outputDim) containing output from network
        labels: 2 dimensional Tensor containing all the targets of the batch with zero padded
        act_lens: Tensor of size (batch) containing size of each output sequence from the network
        label_lens: Tensor of (batch) containing label length of each example
        fastemit_lambda: Float scaling factor for FastEmit regularization. Refer to
            FastEmit: Low-latency Streaming ASR with Sequence-level Emission Regularization.
        """
        is_cuda = acts.is_cuda

        certify_inputs(acts, labels, act_lens, label_lens)
        if clamp < 0:
            raise ValueError("`clamp` must be 0.0 or positive float value.")

        loss_func = rnnt_dep.rnnt_loss_gpu if is_cuda else rnnt_dep.rnnt_loss_cpu
        grads = torch.zeros_like(acts) if acts.requires_grad else None
        minibatch_size = acts.size(0)
        costs = torch.zeros(minibatch_size, device=acts.device, dtype=torch.float32)

        loss_func(
            acts,
            labels=labels,
            input_lengths=act_lens,
            label_lengths=label_lens,
            costs=costs,
            grads=grads,
            blank_label=blank,
            fastemit_lambda=fastemit_lambda,
            clamp=clamp,
            num_threads=0,
        )

        if reduction in ['sum', 'mean']:
            costs = costs.sum().unsqueeze_(-1)
            if reduction == 'mean':
                costs /= minibatch_size

                if grads is not None:
                    grads /= minibatch_size

        ctx.grads = grads

        return costs

    @staticmethod
    def backward(ctx, grad_output):
        if grad_output is not None and ctx.grads is not None:
            grad_output = grad_output.view(-1, 1, 1, 1).to(ctx.grads)
            return ctx.grads.mul_(grad_output), None, None, None, None, None, None, None


class _TDTNumba_dep(Function):
    """
    Numba class for Token-and-Duration Transducer (TDT) loss (https://arxiv.org/abs/2304.06795)
    """

    @staticmethod
    def forward(
        ctx,
        label_acts,
        duration_acts,
        labels,
        act_lens,
        label_lens,
        blank,
        durations,
        reduction,
        fastemit_lambda,
        clamp,
        sigma,
        omega,
    ):
        """
        log_probs: Tensor of (batch x seqLength x labelLength x outputDim) containing output from network
        labels: 2 dimensional Tensor containing all the targets of the batch with zero padded
        act_lens: Tensor of size (batch) containing size of each output sequence from the network
        label_lens: Tensor of (batch) containing label length of each example
        fastemit_lambda: Float scaling factor for FastEmit regularization. Refer to
            FastEmit: Low-latency Streaming ASR with Sequence-level Emission Regularization.
        durations: list of durations for TDT model, must include 0 and 1, e.g.
            [0, 1, 2, 3, 4].
        sigma: hyper-parameter for logit under-normalization method for training
            TDT models. Recommended value 0.05.
        omega: probability for sampling the standard RNN-T loss.
        Refer to https://arxiv.org/abs/2304.06795 for detailed explanations for
            the above parameters;
        """
        is_cuda = label_acts.is_cuda

        certify_inputs(label_acts, labels, act_lens, label_lens)
        if clamp < 0:
            raise ValueError("`clamp` must be 0.0 or positive float value.")

        if is_cuda:
            loss_func = rnnt_dep.tdt_loss_gpu_dep
        else:
            raise ValueError("TDT is not yet implemented for non CUDA computation.")

        label_grads = torch.zeros_like(label_acts) if label_acts.requires_grad else None
        duration_grads = torch.zeros_like(duration_acts) if duration_acts.requires_grad else None

        minibatch_size = label_acts.size(0)
        costs = torch.zeros(minibatch_size, device=label_acts.device, dtype=label_acts.dtype)

        loss_func(
            label_acts,
            duration_acts,
            labels=labels,
            input_lengths=act_lens,
            label_lengths=label_lens,
            costs=costs,
            label_grads=label_grads,
            duration_grads=duration_grads,
            blank_label=blank,
            durations=durations,
            fastemit_lambda=fastemit_lambda,
            clamp=clamp,
            sigma=sigma,
            omega=omega,
            num_threads=0,
        )

        if reduction in ['sum', 'mean']:
            costs = costs.sum().unsqueeze_(-1)
            if reduction == 'mean':
                costs /= minibatch_size

                if label_grads is not None:
                    label_grads /= minibatch_size
                    duration_grads /= minibatch_size

        ctx.label_grads = label_grads
        ctx.duration_grads = duration_grads

        # print(labels[0])
        # print(label_lens)
        return costs

    @staticmethod
    def backward(ctx, grad_output):
        if grad_output is not None and ctx.label_grads is not None:
            # print(ctx.label_grads[0, :, 108, 0])
            # print(ctx.label_grads[0, :, 109, 129 + 0])
            # print(ctx.label_grads[0, :, 109, 129*2 + 0])
            # print(ctx.label_grads[0, :, 109, 129*3 + 0])
            # print(ctx.label_grads[0, :, 109, 129*4 + 0])
            # print(grad_output)
            grad_output = grad_output.view(-1, 1, 1, 1).to(ctx.label_grads)
            return (
                ctx.label_grads.mul_(grad_output),
                # jykang
                ctx.duration_grads.mul_(grad_output),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )


class _MultiblankRNNTNumba_dep(Function):
    """
    Numba class for multi-blank transducer loss (https://arxiv.org/pdf/2211.03541.pdf)
    """

    @staticmethod
    def forward(
        ctx, acts, labels, act_lens, label_lens, blank, big_blank_durations, reduction, fastemit_lambda, clamp, sigma
    ):
        """
        big_blank_durations: list of durations for multi-blank transducer, e.g.
            [2, 4, 8].
        sigma: hyper-parameter for logit under-normalization method for training
            multi-blank transducers. Recommended value 0.05.
        Refer to https://arxiv.org/pdf/2211.03541 for detailed explanations for
            the above parameters;
        For other parameters for this class, refer to comment for class _RNNTNumba
        """
        is_cuda = acts.is_cuda

        certify_inputs(acts, labels, act_lens, label_lens)
        if clamp < 0:
            raise ValueError("`clamp` must be 0.0 or positive float value.")

        if is_cuda:
            loss_func = rnnt_dep.multiblank_rnnt_loss_gpu
        else:
            raise NotImplementedError()

        grads = torch.zeros_like(acts) if acts.requires_grad else None
        minibatch_size = acts.size(0)
        costs = torch.zeros(minibatch_size, device=acts.device, dtype=acts.dtype)

        loss_func(
            acts,
            labels=labels,
            input_lengths=act_lens,
            label_lengths=label_lens,
            costs=costs,
            grads=grads,
            blank_label=blank,
            big_blank_durations=big_blank_durations,
            fastemit_lambda=fastemit_lambda,
            clamp=clamp,
            sigma=sigma,
            num_threads=0,
        )

        if reduction in ['sum', 'mean']:
            costs = costs.sum().unsqueeze_(-1)
            if reduction == 'mean':
                costs /= minibatch_size

                if grads is not None:
                    grads /= minibatch_size

        ctx.grads = grads

        return costs

    @staticmethod
    def backward(ctx, grad_output):
        if grad_output is not None and ctx.grads is not None:
            grad_output = grad_output.view(-1, 1, 1, 1).to(ctx.grads)
            return ctx.grads.mul_(grad_output), None, None, None, None, None, None, None, None, None, None


def rnnt_loss(
    acts, labels, act_lens, label_lens, blank=0, reduction='mean', fastemit_lambda: float = 0.0, clamp: float = 0.0
):
    """RNN Transducer Loss (functional form)
    Args:
        acts: Tensor of (batch x seqLength x labelLength x outputDim) containing output from network
        labels: 2 dimensional Tensor containing all the targets of the batch with zero padded
        act_lens: Tensor of size (batch) containing size of each output sequence from the network
        label_lens: Tensor of (batch) containing label length of each example
        blank (int, optional): blank label. Default: 0.
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the output losses will be divided by the target lengths and
            then the mean over the batch is taken. Default: 'mean'
    """
    if not acts.is_cuda:
        # Since CPU requires log_softmax to be computed explicitly, we need to perform grad clipping
        # *after* we have obtained the gradients of loss(logsoftmax()).
        # This is highly wasteful since it requires a copy of the entire joint tensor which is expensive.
        # CUDA version is much more efficient since it performs an inplace logsoftmax, and therefore
        # can inplace clamp the gradient.
        if clamp > 0.0:
            acts = cpu_rnnt.LogSoftmaxGradModification.apply(acts, clamp)

        # NOTE manually done log_softmax for CPU version,
        # log_softmax is computed within GPU version.
        acts = torch.nn.functional.log_softmax(acts, -1)

    return _RNNTNumba_dep.apply(acts, labels, act_lens, label_lens, blank, reduction, fastemit_lambda, clamp)


def multiblank_rnnt_loss(
    acts,
    labels,
    act_lens,
    label_lens,
    blank,
    big_blank_durations=[],
    reduction='mean',
    fastemit_lambda: float = 0.0,
    clamp: float = 0.0,
):
    """
    Multi-blank RNN Transducer (https://arxiv.org/pdf/2211.03541.pdf) Loss (functional form)
    Args:
        acts: Tensor of (batch x seqLength x labelLength x outputDim) containing output from network
        labels: 2 dimensional Tensor containing all the targets of the batch with zero padded
        act_lens: Tensor of size (batch) containing size of each output sequence from the network
        label_lens: Tensor of (batch) containing label length of each example
        blank (int): standard blank label.
        big_blank_durations: list of durations for multi-blank transducer, e.g.
            [2, 4, 8].
        sigma: hyper-parameter for logit under-normalization method for training
            multi-blank transducers. Recommended value 0.05.
        Refer to https://arxiv.org/pdf/2211.03541 for detailed explanations for
            the last two params.
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the output losses will be divided by the target lengths and
            then the mean over the batch is taken. Default: 'mean'
    """
    if not acts.is_cuda:
        # Since CPU requires log_softmax to be computed explicitly, we need to perform grad clipping
        # *after* we have obtained the gradients of loss(logsoftmax()).
        # This is highly wasteful since it requires a copy of the entire joint tensor which is expensive.
        # CUDA version is much more efficient since it performs an inplace logsoftmax, and therefore
        # can inplace clamp the gradient.
        if clamp > 0.0:
            acts = cpu_rnnt.LogSoftmaxGradModification.apply(acts, clamp)

        # NOTE manually done log_softmax for CPU version,
        # log_softmax is computed within GPU version.
        acts = torch.nn.functional.log_softmax(acts, -1)

    return _MultiblankRNNTNumba_dep.apply(
        acts, labels, act_lens, label_lens, blank, big_blank_durations, reduction, fastemit_lambda, clamp
    )


def tdt_loss(
    acts,
    labels,
    act_lens,
    label_lens,
    blank,
    durations=[],
    reduction='mean',
    fastemit_lambda: float = 0.0,
    clamp: float = 0.0,
):
    """
    TDT RNN Transducer (https://arxiv.org/abs/2304.06795) Loss (functional form)
    Args:
        acts: Tensor of (batch x seqLength x labelLength x outputDim) containing output from network
        labels: 2 dimensional Tensor containing all the targets of the batch with zero padded
        act_lens: Tensor of size (batch) containing size of each output sequence from the network
        label_lens: Tensor of (batch) containing label length of each example
        blank (int): standard blank label.
        durations: list of durations for TDT model, e.g.
            [0,1,2,3,4].
        sigma: hyper-parameter for logit under-normalization method for training
            multi-blank transducers. Recommended value 0.05.
        Refer to https://arxiv.org/abs/2304.06795 for detailed explanations for
            the last two params.
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the output losses will be divided by the target lengths and
            then the mean over the batch is taken. Default: 'mean'
    """
    if not acts.is_cuda:
        # Since CPU requires log_softmax to be computed explicitly, we need to perform grad clipping
        # *after* we have obtained the gradients of loss(logsoftmax()).
        # This is highly wasteful since it requires a copy of the entire joint tensor which is expensive.
        # CUDA version is much more efficient since it performs an inplace logsoftmax, and therefore
        # can inplace clamp the gradient.
        if clamp > 0.0:
            acts = cpu_rnnt.LogSoftmaxGradModification.apply(acts, clamp)

        # NOTE manually done log_softmax for CPU version,
        # log_softmax is computed within GPU version.
        acts = torch.nn.functional.log_softmax(acts, -1)

    return _TDTNumba_dep.apply(acts, labels, act_lens, label_lens, blank, durations, reduction, fastemit_lambda, clamp)


class RNNTLossNumba_dep(Module):
    """
    Parameters:
        blank (int, optional): blank label. Default: 0.
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the output losses will be divided by the target lengths and
            then the mean over the batch is taken. Default: 'mean'
        fastemit_lambda: Float scaling factor for FastEmit regularization. Refer to
                FastEmit: Low-latency Streaming ASR with Sequence-level Emission Regularization.
        clamp: Float value. When set to value >= 0.0, will clamp the gradient to [-clamp, clamp].
    """

    def __init__(self, blank=0, reduction='mean', fastemit_lambda: float = 0.0, clamp: float = -1):
        super(RNNTLossNumba_dep, self).__init__()
        self.blank = blank
        self.fastemit_lambda = fastemit_lambda
        self.clamp = float(clamp) if clamp > 0 else 0.0
        self.reduction = reduction
        self.loss = _RNNTNumba_dep.apply

    def forward(self, acts, labels, act_lens, label_lens):
        """
        log_probs: Tensor of (batch x seqLength x labelLength x outputDim) containing output from network
        labels: 2 dimensional Tensor containing all the targets of the batch with zero padded
        act_lens: Tensor of size (batch) containing size of each output sequence from the network
        label_lens: Tensor of (batch) containing label length of each example
        """
        if not acts.is_cuda:
            # Force FP32 until log_softmax() is implemented for fp16 on CPU
            if acts.dtype == torch.float16:
                acts = acts.float()

            # Since CPU requires log_softmax to be computed explicitly, we need to perform grad clipping
            # *after* we have obtained the gradients of loss(logsoftmax()).
            # This is highly wasteful since it requires a copy of the entire joint tensor which is expensive.
            # CUDA version is much more efficient since it performs an inplace logsoftmax, and therefore
            # can inplace clamp the gradient.
            if self.clamp > 0.0:
                acts = cpu_rnnt.LogSoftmaxGradModification.apply(acts, self.clamp)

            # NOTE manually done log_softmax for CPU version,
            # log_softmax is computed within GPU version.
            acts = torch.nn.functional.log_softmax(acts, -1)

        return self.loss(
            acts, labels, act_lens, label_lens, self.blank, self.reduction, self.fastemit_lambda, self.clamp
        )


class MultiblankRNNTLossNumba_dep(Module):
    """
    Parameters:
        blank (int): standard blank label.
        big_blank_durations: list of durations for multi-blank transducer, e.g.
            [2, 4, 8].
        sigma: hyper-parameter for logit under-normalization method for training
            multi-blank transducers. Recommended value 0.05.
        Refer to https://arxiv.org/pdf/2211.03541 for detailed explanations for
            the above parameters;
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the output losses will be divided by the target lengths and
            then the mean over the batch is taken. Default: 'mean'
        fastemit_lambda: Float scaling factor for FastEmit regularization. Refer to
                FastEmit: Low-latency Streaming ASR with Sequence-level Emission Regularization.
        clamp: Float value. When set to value >= 0.0, will clamp the gradient to [-clamp, clamp].
    """

    def __init__(
        self,
        blank,
        big_blank_durations,
        reduction='mean',
        fastemit_lambda: float = 0.0,
        clamp: float = -1,
        sigma: float = 0.0,
    ):
        super(MultiblankRNNTLossNumba_dep, self).__init__()
        self.blank = blank
        self.big_blank_durations = big_blank_durations
        self.fastemit_lambda = fastemit_lambda
        self.clamp = float(clamp) if clamp > 0 else 0.0
        self.reduction = reduction
        self.loss = _MultiblankRNNTNumba_dep.apply
        self.sigma = sigma

    def forward(self, acts, labels, act_lens, label_lens):
        """
        log_probs: Tensor of (batch x seqLength x labelLength x outputDim) containing output from network
        labels: 2 dimensional Tensor containing all the targets of the batch with zero padded
        act_lens: Tensor of size (batch) containing size of each output sequence from the network
        label_lens: Tensor of (batch) containing label length of each example
        """
        if not acts.is_cuda:
            # Since CPU requires log_softmax to be computed explicitly, we need to perform grad clipping
            # *after* we have obtained the gradients of loss(logsoftmax()).
            # This is highly wasteful since it requires a copy of the entire joint tensor which is expensive.
            # CUDA version is much more efficient since it performs an inplace logsoftmax, and therefore
            # can inplace clamp the gradient.
            if self.clamp > 0.0:
                acts = cpu_rnnt.LogSoftmaxGradModification.apply(acts, self.clamp)

            # NOTE manually done log_softmax for CPU version,
            # log_softmax is computed within GPU version.
            acts = torch.nn.functional.log_softmax(acts, -1)

        return self.loss(
            acts,
            labels,
            act_lens,
            label_lens,
            self.blank,
            self.big_blank_durations,
            self.reduction,
            self.fastemit_lambda,
            self.clamp,
            self.sigma,
        )


class TDTLossNumba_dep(Module):
    """
    Parameters:
        blank (int): standard blank label.
        durations: list of durations for TDT model, e.g.
            [0, 1, 2, 3, 4].
        sigma: hyper-parameter for logit under-normalization method for training
            TDT. Recommended value 0.05.
        omega: hyper-parameter for RNN-T loss for loss combination.
        Refer to https://arxiv.org/abs/2304.06795 for detailed explanations for
            the above parameters;

        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the output losses will be divided by the target lengths and
            then the mean over the batch is taken. Default: 'mean'
        fastemit_lambda: Float scaling factor for FastEmit regularization. Refer to
                FastEmit: Low-latency Streaming ASR with Sequence-level Emission Regularization.
        clamp: Float value. When set to value >= 0.0, will clamp the gradient to [-clamp, clamp].
    """

    def __init__(
        self,
        blank,
        durations=None,
        reduction='mean',
        fastemit_lambda: float = 0.0,
        clamp: float = -1,
        sigma: float = 0.0,
        omega: float = 0.0,
    ):
        super(TDTLossNumba_dep, self).__init__()
        self.blank = blank
        self.durations = durations if durations is not None else []
        self.fastemit_lambda = fastemit_lambda
        self.clamp = float(clamp) if clamp > 0 else 0.0
        self.reduction = reduction
        self.loss = _TDTNumba_dep.apply
        self.sigma = sigma
        self.omega = omega

    def forward(self, acts, labels, act_lens, label_lens):
        """
        log_probs: Tensor of (batch x seqLength x labelLength x outputDim) containing output from network
        labels: 2 dimensional Tensor containing all the targets of the batch with zero padded
        act_lens: Tensor of size (batch) containing size of each output sequence from the network
        label_lens: Tensor of (batch) containing label length of each example
        """
        # TODO(hainan): in the future, we could further optimize this so that we don't need to
        # make contiguous copies of the acts tensor.
        # label_acts, duration_acts = torch.split(
        #     acts, [acts.shape[-1] - len(self.durations), len(self.durations)], dim=-1
        # )
        # print(acts.shape) -> (B, T, U, V)
        # print(self.durations) -> (0, 1, 2, 3, 4)
        # print(label_acts.shape) -> (B, T, U, V-5)
        # print(duration_acts.shape) -> (B, T, U, 5)


        acts = torch.nn.functional.log_softmax(acts, dim=-1).contiguous()
        # acts = acts.contiguous()
        duration_acts = torch.zeros((acts.shape[0],1,1,1), requires_grad=True).contiguous().cuda()

        # label_acts = label_acts.contiguous()
        # duration_acts = torch.nn.functional.log_softmax(duration_acts, dim=-1).contiguous()

        return self.loss(
            # label_acts,
            acts,
            duration_acts,
            labels,
            act_lens,
            label_lens,
            self.blank,
            self.durations,
            self.reduction,
            self.fastemit_lambda,
            self.clamp,
            self.sigma,
            self.omega,
        )


def check_type(var, t, name):
    if var.dtype is not t:
        raise TypeError("{} must be {}".format(name, t))


def check_contiguous(var, name):
    if not var.is_contiguous():
        raise ValueError("{} must be contiguous".format(name))


def check_dim(var, dim, name):
    if len(var.shape) != dim:
        raise ValueError("{} must be {}D".format(name, dim))


def certify_inputs(log_probs, labels, lengths, label_lengths):
    # check_type(log_probs, torch.float32, "log_probs")
    check_type(labels, torch.int64, "labels")
    check_type(label_lengths, torch.int64, "label_lengths")
    check_type(lengths, torch.int64, "lengths")
    check_contiguous(log_probs, "log_probs")
    check_contiguous(labels, "labels")
    check_contiguous(label_lengths, "label_lengths")
    check_contiguous(lengths, "lengths")

    if lengths.shape[0] != log_probs.shape[0]:
        raise ValueError(
            f"Must have a length per example. "
            f"Given lengths dim: {lengths.shape[0]}, "
            f"Log probs dim : {log_probs.shape[0]}"
        )
    if label_lengths.shape[0] != log_probs.shape[0]:
        raise ValueError(
            "Must have a label length per example. "
            f"Given label lengths dim : {label_lengths.shape[0]}, "
            f"Log probs dim : {log_probs.shape[0]}"
        )

    check_dim(log_probs, 4, "log_probs")
    check_dim(labels, 2, "labels")
    check_dim(lengths, 1, "lenghts")
    check_dim(label_lengths, 1, "label_lenghts")
    max_T = torch.max(lengths)
    max_U = torch.max(label_lengths)
    T, U = log_probs.shape[1:3]
    if T != max_T:
        raise ValueError(f"Input length mismatch! Given T: {T}, Expected max T from input lengths: {max_T}")
    if U != max_U + 1:
        raise ValueError(f"Output length mismatch! Given U: {U}, Expected max U from target lengths: {max_U} + 1")
