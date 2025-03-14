# Owner(s): ["oncall: quantization"]
import copy
import operator
import unittest
from typing import Any, Optional, Tuple

import torch
from torch._export import capture_pre_autograd_graph
from torch.ao.quantization import (
    default_fake_quant,
    FusedMovingAvgObsFakeQuantize,
    MovingAverageMinMaxObserver,
    MovingAveragePerChannelMinMaxObserver,
    QConfigMapping,
)
from torch.ao.quantization.backend_config import get_qnnpack_backend_config
from torch.ao.quantization.qconfig import (
    default_per_channel_symmetric_qnnpack_qat_qconfig,
    default_symmetric_qnnpack_qat_qconfig,
)
from torch.ao.quantization.quantize_fx import prepare_qat_fx
from torch.ao.quantization.quantize_pt2e import (
    _convert_to_reference_decomposed_fx,
    convert_pt2e,
    prepare_pt2e,
    prepare_qat_pt2e,
)
from torch.ao.quantization.quantizer import (
    DerivedQuantizationSpec,
    QuantizationAnnotation,
    QuantizationSpec,
    Quantizer,
)
from torch.ao.quantization.quantizer.xnnpack_quantizer import (
    get_symmetric_quantization_config,
    XNNPACKQuantizer,
)
from torch.testing._internal.common_cuda import TEST_CUDA
from torch.testing._internal.common_quantization import (
    NodeSpec as ns,
    QuantizationTestCase,
    skip_if_no_torchvision,
    skipIfNoQNNPACK,
    TestHelperModules,
)
from torch.testing._internal.common_quantized import override_quantized_engine


class PT2EQATTestCase(QuantizationTestCase):
    """
    Base QuantizationTestCase for PT2E QAT with some helper methods.
    """

    def _verify_symmetric_xnnpack_qat_numerics(
        self,
        model: torch.nn.Module,
        example_inputs: Tuple[Any, ...],
    ):
        self._verify_symmetric_xnnpack_qat_numerics_helper(
            model,
            example_inputs,
            is_per_channel=True,
        )
        self._verify_symmetric_xnnpack_qat_numerics_helper(
            model,
            example_inputs,
            is_per_channel=False,
        )

    def _verify_symmetric_xnnpack_qat_numerics_helper(
        self,
        model: torch.nn.Module,
        example_inputs: Tuple[Any, ...],
        is_per_channel: bool,
        verify_convert: bool = True,
    ):
        """
        Helper method to verify that the QAT numerics for PT2E quantization match those of
        FX graph mode quantization for symmetric qnnpack.
        """
        # resetting dynamo cache
        torch._dynamo.reset()
        MANUAL_SEED = 100

        # PT2 export

        model_pt2e = copy.deepcopy(model)
        quantizer = XNNPACKQuantizer()
        quantizer.set_global(
            get_symmetric_quantization_config(
                is_per_channel=is_per_channel, is_qat=True
            )
        )
        model_pt2e = capture_pre_autograd_graph(
            model_pt2e,
            example_inputs,
        )
        model_pt2e = prepare_qat_pt2e(model_pt2e, quantizer)
        torch.manual_seed(MANUAL_SEED)
        after_prepare_result_pt2e = model_pt2e(*example_inputs)

        model_fx = copy.deepcopy(model)
        if is_per_channel:
            default_qconfig = default_per_channel_symmetric_qnnpack_qat_qconfig
        else:
            default_qconfig = default_symmetric_qnnpack_qat_qconfig
        qconfig_mapping = QConfigMapping().set_global(default_qconfig)
        backend_config = get_qnnpack_backend_config()
        model_fx = prepare_qat_fx(
            model_fx, qconfig_mapping, example_inputs, backend_config=backend_config
        )
        torch.manual_seed(MANUAL_SEED)
        after_prepare_result_fx = model_fx(*example_inputs)

        # Verify that numerics match
        self.assertEqual(after_prepare_result_pt2e, after_prepare_result_fx)

        if verify_convert:
            torch.ao.quantization.move_exported_model_to_eval(model_pt2e)
            model_pt2e = convert_pt2e(model_pt2e)
            quant_result_pt2e = model_pt2e(*example_inputs)
            model_fx.eval()
            model_fx = _convert_to_reference_decomposed_fx(
                model_fx,
                backend_config=backend_config,
            )
            quant_result_fx = model_fx(*example_inputs)
            self.assertEqual(quant_result_pt2e, quant_result_fx)

    def _verify_symmetric_xnnpack_qat_graph(
        self,
        m: torch.fx.GraphModule,
        example_inputs: Tuple[Any, ...],
        has_relu: bool,
        has_bias: bool = True,
        is_cuda: bool = False,
        expected_conv_literal_args: Optional[Tuple[Any, ...]] = None,
    ):
        self._verify_symmetric_xnnpack_qat_graph_helper(
            m,
            example_inputs,
            is_per_channel=True,
            has_relu=has_relu,
            has_bias=has_bias,
            is_cuda=is_cuda,
            expected_conv_literal_args=expected_conv_literal_args,
        )
        self._verify_symmetric_xnnpack_qat_graph_helper(
            m,
            example_inputs,
            is_per_channel=False,
            has_relu=has_relu,
            has_bias=has_bias,
            is_cuda=is_cuda,
            expected_conv_literal_args=expected_conv_literal_args,
        )

    def _verify_symmetric_xnnpack_qat_graph_helper(
        self,
        m: torch.fx.GraphModule,
        example_inputs: Tuple[Any, ...],
        is_per_channel: bool,
        has_relu: bool,
        has_bias: bool = True,
        is_cuda: bool = False,
        expected_conv_literal_args: Optional[Tuple[Any, ...]] = None,
    ):
        """
        Verify that the graph module matches the fused QAT [conv - bn (- relu)] pattern
        with fake quantizes inserted into the correct places.
        # TODO: also verify that metadata is copied over to the new nodes.
        """
        m = copy.deepcopy(m)
        quantizer = XNNPACKQuantizer()
        quantizer.set_global(
            get_symmetric_quantization_config(is_per_channel, is_qat=True)
        )
        m = capture_pre_autograd_graph(
            m,
            example_inputs,
        )
        m = prepare_qat_pt2e(m, quantizer)
        m(*example_inputs)

        # Verify: getitem output activation fake quantize
        output_node = list(m.graph.nodes)[-1]
        output_fq_node = output_node.args[0][0]
        self.assertTrue(output_fq_node.target.startswith("activation_post_process_"))
        output_fq_mod = getattr(m, output_fq_node.target)
        self.assertEqual(type(output_fq_mod), FusedMovingAvgObsFakeQuantize)
        self.assertEqual(
            type(output_fq_mod.activation_post_process), MovingAverageMinMaxObserver
        )
        self.assertEqual(output_fq_mod.dtype, torch.int8)
        self.assertEqual(output_fq_mod.quant_min, -128)
        self.assertEqual(output_fq_mod.quant_max, 127)

        # Verify: getitem(bn, 0) or relu(getitem(bn, 0))
        if has_relu:
            relu_node = output_fq_node.args[0]
            getitem_node = relu_node.args[0]
            self.assertEqual(relu_node.target, torch.ops.aten.relu.default)
        else:
            relu_node = None
            getitem_node = output_fq_node.args[0]
        bn_node = getitem_node.args[0]
        if is_cuda:
            if torch.version.cuda is not None:
                expected_bn_op = torch.ops.aten.cudnn_batch_norm.default
            elif torch.version.hip is not None:
                expected_bn_op = torch.ops.aten.miopen_batch_norm.default
        else:
            expected_bn_op = torch.ops.aten._native_batch_norm_legit.default
        self.assertEqual(getitem_node.target, operator.getitem)
        self.assertEqual(bn_node.target, expected_bn_op)

        # Verify: conv / scale_factor.reshape [+ bias.reshape]
        if has_bias:
            add_bias_node = bn_node.args[0]
            (div_scale_factor_node, bias_reshape_node) = add_bias_node.args
            self.assertEqual(add_bias_node.target, torch.ops.aten.add.Tensor)
            self.assertEqual(bias_reshape_node.target, torch.ops.aten.reshape.default)
        else:
            div_scale_factor_node = bn_node.args[0]
        (conv_node, scale_factor_reshape_node) = div_scale_factor_node.args
        self.assertEqual(div_scale_factor_node.target, torch.ops.aten.div.Tensor)
        self.assertEqual(conv_node.target, torch.ops.aten.conv2d.default)
        self.assertEqual(
            scale_factor_reshape_node.target, torch.ops.aten.reshape.default
        )

        # Verify: conv literal args
        if expected_conv_literal_args is not None:
            assert (
                len(expected_conv_literal_args) == 6
            ), "wrong num conv args, bad test setup"
            for i in range(6):
                if i + 3 < len(conv_node.args):
                    self.assertEqual(
                        conv_node.args[i + 3], expected_conv_literal_args[i]
                    )

        # Verify: conv input activation fake quantize
        conv_input_fq_node = conv_node.args[0]
        conv_input_node = conv_input_fq_node.args[0]
        self.assertTrue(
            conv_input_fq_node.target.startswith("activation_post_process_")
        )
        conv_input_fq_mod = getattr(m, conv_input_fq_node.target)
        self.assertEqual(type(conv_input_fq_mod), FusedMovingAvgObsFakeQuantize)
        self.assertEqual(
            type(conv_input_fq_mod.activation_post_process), MovingAverageMinMaxObserver
        )
        self.assertEqual(conv_input_fq_mod.dtype, torch.int8)
        self.assertEqual(conv_input_fq_mod.quant_min, -128)
        self.assertEqual(conv_input_fq_mod.quant_max, 127)
        self.assertTrue(conv_input_node.op, "placeholder")

        # Verify: conv weight fake quantize
        conv_weight_fq_node = conv_node.args[1]
        self.assertTrue(
            conv_weight_fq_node.target.startswith("activation_post_process_")
        )
        conv_weight_fq_mod = getattr(m, conv_weight_fq_node.target)
        if is_per_channel:
            expected_weight_observer_type = MovingAveragePerChannelMinMaxObserver
        else:
            expected_weight_observer_type = MovingAverageMinMaxObserver
        self.assertEqual(type(conv_weight_fq_mod), FusedMovingAvgObsFakeQuantize)
        self.assertEqual(
            type(conv_weight_fq_mod.activation_post_process),
            expected_weight_observer_type,
        )
        self.assertEqual(conv_weight_fq_mod.dtype, torch.int8)
        self.assertEqual(conv_weight_fq_mod.quant_min, -127)
        self.assertEqual(conv_weight_fq_mod.quant_max, 127)

        # Verify: conv(fq(input), fq(weight * scale_factor.reshape), zero_bias)
        zero_bias_node = conv_node.args[2] if len(conv_node.args) > 2 else None
        mul_weight_scale_factor_node = conv_weight_fq_node.args[0]
        (
            conv_weight_fq_node,
            scale_factor_reshape_node,
        ) = mul_weight_scale_factor_node.args
        if has_bias:
            self.assertEqual(zero_bias_node.target, torch.ops.aten.zeros_like.default)
        else:
            self.assertTrue(zero_bias_node is None)
        self.assertEqual(mul_weight_scale_factor_node.target, torch.ops.aten.mul.Tensor)
        self.assertEqual(
            scale_factor_reshape_node.target, torch.ops.aten.reshape.default
        )

        # Verify: scale_factor = bn_weight / sqrt(bn_running_var + eps)
        scale_factor_node = scale_factor_reshape_node.args[0]
        (bn_weight_node, sqrt_node) = scale_factor_node.args
        bn_running_var_add_node = sqrt_node.args[0]
        (bn_running_var_node, eps) = bn_running_var_add_node.args
        self.assertEqual(scale_factor_node.target, torch.ops.aten.div.Tensor)
        self.assertTrue("param_constant" in bn_weight_node.target)
        self.assertEqual(sqrt_node.target, torch.ops.aten.sqrt.default)
        self.assertEqual(bn_running_var_add_node.target, torch.ops.aten.add.Tensor)
        self.assertTrue("tensor_constant" in bn_running_var_node.target)
        self.assertEqual(eps, 1e-5)


@skipIfNoQNNPACK
class TestQuantizePT2EQAT(PT2EQATTestCase):
    def test_qat_conv_no_bias(self):
        class M(torch.nn.Module):
            def __init__(self, has_relu: bool):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 3, 3, bias=False)
                self.relu = torch.nn.ReLU() if has_relu else torch.nn.Identity()

            def forward(self, x):
                x = self.conv(x)
                x = self.relu(x)
                return x

        example_inputs = (torch.randn(1, 3, 5, 5),)
        self._verify_symmetric_xnnpack_qat_numerics(M(has_relu=False), example_inputs)
        self._verify_symmetric_xnnpack_qat_numerics(M(has_relu=True), example_inputs)

    def test_qat_conv_bn_fusion(self):
        m = TestHelperModules.ConvWithBNRelu(relu=False)
        example_inputs = (torch.randn(1, 3, 5, 5),)
        self._verify_symmetric_xnnpack_qat_graph(m, example_inputs, has_relu=False)
        self._verify_symmetric_xnnpack_qat_numerics(m, example_inputs)

    @unittest.skipIf(not TEST_CUDA, "CUDA unavailable")
    def test_qat_conv_bn_fusion_cuda(self):
        m = TestHelperModules.ConvWithBNRelu(relu=False).cuda()
        example_inputs = (torch.randn(1, 3, 5, 5).cuda(),)
        self._verify_symmetric_xnnpack_qat_graph(
            m,
            example_inputs,
            has_relu=False,
            is_cuda=True,
        )
        self._verify_symmetric_xnnpack_qat_numerics(m, example_inputs)

    def test_qat_conv_bn_fusion_literal_args(self):
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 3, 3, stride=(2, 2), padding=(4, 4))
                self.bn = torch.nn.BatchNorm2d(3)

            def forward(self, x):
                x = self.conv(x)
                x = self.bn(x)
                return x

        example_inputs = (torch.randn(1, 3, 5, 5),)
        # stride, padding, dilation, transposed, output_padding, groups
        conv_args = ((2, 2), (4, 4), (1, 1), False, (0, 0), 1)
        self._verify_symmetric_xnnpack_qat_graph(
            M(),
            example_inputs,
            has_relu=False,
            expected_conv_literal_args=conv_args,
        )
        self._verify_symmetric_xnnpack_qat_numerics(M(), example_inputs)

    def test_qat_conv_bn_fusion_no_conv_bias(self):
        class M2(torch.nn.Module):
            """
            Mixed conv + BN with and without conv bias.
            """

            def __init__(self):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(3, 3, 3, bias=False)
                self.bn1 = torch.nn.BatchNorm2d(3)
                self.conv2 = torch.nn.Conv2d(3, 3, 3, bias=True)
                self.bn2 = torch.nn.BatchNorm2d(3)

            def forward(self, x):
                x = self.conv1(x)
                x = self.bn1(x)
                x = self.conv2(x)
                x = self.bn2(x)
                return x

        m1 = TestHelperModules.ConvWithBNRelu(relu=False, bias=False)
        example_inputs = (torch.randn(3, 3, 5, 5),)
        self._verify_symmetric_xnnpack_qat_graph(
            m1,
            example_inputs,
            has_relu=False,
            has_bias=False,
        )
        self._verify_symmetric_xnnpack_qat_numerics(m1, example_inputs)
        self._verify_symmetric_xnnpack_qat_numerics(M2(), example_inputs)

    def test_qat_conv_bn_relu_fusion(self):
        m = TestHelperModules.ConvWithBNRelu(relu=True)
        example_inputs = (torch.randn(1, 3, 5, 5),)
        self._verify_symmetric_xnnpack_qat_graph(m, example_inputs, has_relu=True)
        self._verify_symmetric_xnnpack_qat_numerics(m, example_inputs)

    @unittest.skipIf(not TEST_CUDA, "CUDA unavailable")
    def test_qat_conv_bn_relu_fusion_cuda(self):
        m = TestHelperModules.ConvWithBNRelu(relu=True).cuda()
        example_inputs = (torch.randn(1, 3, 5, 5).cuda(),)
        self._verify_symmetric_xnnpack_qat_graph(
            m,
            example_inputs,
            has_relu=True,
            is_cuda=True,
        )
        self._verify_symmetric_xnnpack_qat_numerics(m, example_inputs)

    def test_qat_conv_bn_relu_fusion_no_conv_bias(self):
        m = TestHelperModules.ConvWithBNRelu(relu=True, bias=False)
        example_inputs = (torch.randn(3, 3, 5, 5),)
        self._verify_symmetric_xnnpack_qat_graph(
            m,
            example_inputs,
            has_relu=True,
            has_bias=False,
        )
        self._verify_symmetric_xnnpack_qat_numerics(m, example_inputs)

    def test_qat_inplace_add_relu(self):
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(1, 1, 1)
                self.relu = torch.nn.ReLU(inplace=True)

            def forward(self, x):
                x0 = x
                x = self.conv(x)
                x += x0
                x = self.relu(x)
                return x

        example_inputs = (torch.randn(1, 1, 3, 3),)
        self._verify_symmetric_xnnpack_qat_numerics(M(), example_inputs)

    def test_prepare_qat_conv_bn_fusion_getitem_placeholder(self):
        """
        Test the case where the placeholder node for the [conv - bn - getitem] pattern
        is also a getitem node:

          some_op -> unrelated_getitem -> conv -> bn -> conv_bn_getitem

        We want the metadata to be copied from the `conv_bn_getitem` node, not from
        the `unrelated_getitem` node, which is not part of the conv-bn pattern but
        is returned as part of the match anyway (as a placeholder).
        """

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.bn1 = torch.nn.BatchNorm2d(3)
                self.conv = torch.nn.Conv2d(3, 3, 3)
                self.bn2 = torch.nn.BatchNorm2d(3)

            def forward(self, x):
                x = self.bn1(x)
                x = self.conv(x)
                x = self.bn2(x)
                return x

        def _get_getitem_nodes(m: torch.fx.GraphModule):
            """
            Return a 2-tuple of (unrelated_getitem_node, conv_bn_getitem_node) from the graph.
            """
            unrelated_getitem_node, conv_bn_getitem_node = None, None
            for node in m.graph.nodes:
                if (
                    node.target != operator.getitem
                    or node.args[0].target
                    != torch.ops.aten._native_batch_norm_legit.default
                ):
                    continue
                if node.args[0].args[0].op == "placeholder":
                    unrelated_getitem_node = node
                else:
                    conv_bn_getitem_node = node
            assert (
                unrelated_getitem_node is not None
            ), "did not find unrelated getitem node, bad test setup"
            assert (
                conv_bn_getitem_node is not None
            ), "did not find conv bn getitem node, bad test setup"
            return (unrelated_getitem_node, conv_bn_getitem_node)

        # Program capture
        example_inputs = (torch.randn(1, 3, 5, 5),)
        m = capture_pre_autograd_graph(
            M(),
            example_inputs,
        )
        m.graph.eliminate_dead_code()
        m.recompile()
        (_, original_conv_bn_getitem_node) = _get_getitem_nodes(m)

        # Prepare QAT
        quantizer = XNNPACKQuantizer()
        quantizer.set_global(
            get_symmetric_quantization_config(is_per_channel=False, is_qat=True)
        )
        m = prepare_qat_pt2e(m, quantizer)
        (unrelated_getitem_node, conv_bn_getitem_node) = _get_getitem_nodes(m)

        # Verify that the metadata was copied from `conv_bn_getitem`, not `unrelated_getitem`
        original_conv_bn_getitem_meta = original_conv_bn_getitem_node.meta[
            "quantization_annotation"
        ]
        conv_bn_getitem_meta = conv_bn_getitem_node.meta["quantization_annotation"]
        self.assertEqual(conv_bn_getitem_meta, original_conv_bn_getitem_meta)
        self.assertTrue("quantization_annotation" not in unrelated_getitem_node.meta)

    def test_qat_update_shared_qspec(self):
        """
        Test the case where nodes used in SharedQuantizationSpec were replaced
        during QAT subgraph rewriting.
        """

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 3, 3)
                self.bn = torch.nn.BatchNorm2d(3)
                self.hardtanh = torch.nn.Hardtanh()

            def forward(self, x):
                x = self.conv(x)
                x = self.bn(x)
                x = self.hardtanh(x)
                return x

        example_inputs = (torch.randn(1, 3, 5, 5),)
        self._verify_symmetric_xnnpack_qat_numerics(M(), example_inputs)

    def test_qat_preserve_source_fn_stack(self):
        """
        Test whether `source_fn_stack` is preserved after QAT fusion.
        """

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(5, 3, 3)
                self.bn = torch.nn.BatchNorm2d(3)
                self.relu = torch.nn.ReLU()
                self.backbone = TestHelperModules.ConvWithBNRelu(relu=True)

            def forward(self, x):
                x = self.conv(x)
                x = self.bn(x)
                x = self.relu(x)
                x = self.backbone(x)
                return x

        # QAT prepare + convert
        m = M()
        example_inputs = (torch.randn(1, 5, 10, 10),)
        quantizer = XNNPACKQuantizer()
        quantizer.set_global(get_symmetric_quantization_config(is_qat=True))
        m = capture_pre_autograd_graph(m, example_inputs)
        m = prepare_qat_pt2e(m, quantizer)
        m(*example_inputs)
        m = convert_pt2e(m)

        # Extract the conv and relu nodes (bn was folded into conv)
        first_conv, first_relu, second_conv, second_relu = None, None, None, None
        for n in m.graph.nodes:
            if n.target == torch.ops.aten.relu.default:
                if first_relu is None:
                    assert first_conv is None, "bad test setup"
                    first_relu = n
                    first_conv = n.args[0]
                else:
                    assert second_conv is None, "bad test setup"
                    second_relu = n
                    second_conv = n.args[0]

        # Extract the conv weight and bias nodes
        def get_conv_weight_and_bias(conv_node: torch.fx.Node):
            weight_dq_node = conv_node.args[1]
            weight_q_node = weight_dq_node.args[0]
            weight_node = weight_q_node.args[0]
            bias_node = conv_node.args[2]
            assert isinstance(weight_node, torch.fx.Node)
            assert isinstance(bias_node, torch.fx.Node)
            return (weight_node, bias_node)

        first_conv_weight, first_conv_bias = get_conv_weight_and_bias(first_conv)
        second_conv_weight, second_conv_bias = get_conv_weight_and_bias(second_conv)

        # Assert that each set of conv, conv weight, and conv bias are in the same partition
        def get_source_fn(node: torch.fx.Node):
            # E.g. [('l__self___backbone1_conv', <class 'torch.nn.modules.conv.Conv2d'>)]
            return node.meta["source_fn_stack"][0][0]

        self.assertEqual(get_source_fn(first_conv), get_source_fn(first_conv_weight))
        self.assertEqual(get_source_fn(first_conv), get_source_fn(first_conv_bias))
        self.assertEqual(get_source_fn(second_conv), get_source_fn(second_conv_weight))
        self.assertEqual(get_source_fn(second_conv), get_source_fn(second_conv_bias))

        # Assert that different sets of convs and relus have different partitions
        self.assertNotEqual(get_source_fn(first_conv), get_source_fn(first_relu))
        self.assertNotEqual(get_source_fn(first_conv), get_source_fn(second_conv))
        self.assertNotEqual(get_source_fn(second_conv), get_source_fn(second_relu))
        self.assertNotEqual(get_source_fn(first_relu), get_source_fn(second_relu))

        # Assert that "backbone" exists only in the second set of conv and relu's partition
        self.assertTrue("backbone" not in get_source_fn(first_conv))
        self.assertTrue("backbone" not in get_source_fn(first_relu))
        self.assertTrue("backbone" in get_source_fn(second_conv))
        self.assertTrue("backbone" in get_source_fn(second_relu))

    def test_qat_conv_bn_bias_derived_qspec(self):
        m = TestHelperModules.ConvWithBNRelu(relu=False)
        example_inputs = (torch.randn(1, 3, 5, 5),)
        m = capture_pre_autograd_graph(m, example_inputs)
        quantizer = ConvBnDerivedBiasQuantizer()
        m = prepare_qat_pt2e(m, quantizer)
        m(*example_inputs)
        m = convert_pt2e(m)
        m(*example_inputs)

        # Assert that both weight and bias are quantized
        (conv_node, _, _) = _get_conv_bn_getitem_nodes(m)
        weight_dq = conv_node.args[1]
        bias_dq = conv_node.args[2]
        self.assertEqual(
            weight_dq.target,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
        )
        self.assertEqual(
            bias_dq.target,
            torch.ops.quantized_decomposed.dequantize_per_tensor.default,
        )
        weight_q = weight_dq.args[0]
        bias_q = bias_dq.args[0]
        self.assertEqual(
            weight_q.target,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
        )
        self.assertEqual(
            bias_q.target,
            torch.ops.quantized_decomposed.quantize_per_tensor.default,
        )

        # Assert that bias scale = weight scale * input scale
        input_dq = conv_node.args[0]
        input_scale = input_dq.args[1]
        bias_scale = bias_dq.args[1]
        weight_scale = weight_dq.args[1]
        self.assertEqual(bias_scale, input_scale * weight_scale)

        # Assert that args for the bias' quantize and dequantize ops
        # are copied correctly after subgraph rewriting
        (bias_qmin, bias_qmax, bias_dtype) = bias_dq.args[3:]
        self.assertEqual(bias_qmin, -(2**31))
        self.assertEqual(bias_qmax, 2**31 - 1)
        self.assertEqual(bias_dtype, torch.int32)

    def test_qat_per_channel_weight_custom_dtype(self):
        m = TestHelperModules.ConvWithBNRelu(relu=False)
        example_inputs = (torch.randn(1, 3, 5, 5),)
        m = capture_pre_autograd_graph(m, example_inputs)
        quantizer = ConvBnInt32WeightQuantizer()
        m = prepare_qat_pt2e(m, quantizer)
        m(*example_inputs)
        m = convert_pt2e(m)
        m(*example_inputs)

        # Assert that conv weight is quantized per channel
        (conv_node, _, _) = _get_conv_bn_getitem_nodes(m)
        weight_dq = conv_node.args[1]
        self.assertEqual(
            weight_dq.target,
            torch.ops.quantized_decomposed.dequantize_per_channel.default,
        )
        weight_q = weight_dq.args[0]
        self.assertEqual(
            weight_q.target,
            torch.ops.quantized_decomposed.quantize_per_channel.default,
        )

        # Assert that args for the weight's quantize and dequantize ops
        # are copied correctly after subgraph rewriting
        (q_axis, q_qmin, q_qmax, q_dtype) = weight_q.args[3:]
        (dq_axis, dq_qmin, dq_qmax, dq_dtype) = weight_dq.args[3:]
        self.assertEqual(q_axis, 0)
        self.assertEqual(dq_axis, 0)
        self.assertEqual(q_qmin, 0)
        self.assertEqual(dq_qmin, 0)
        self.assertEqual(q_qmax, 2**31 - 1)
        self.assertEqual(dq_qmax, 2**31 - 1)
        self.assertEqual(q_dtype, torch.int32)
        self.assertEqual(dq_dtype, torch.int32)


def _get_conv_bn_getitem_nodes(model: torch.fx.GraphModule):
    """
    Return a 3-tuple of (conv, bn, getitem) nodes from the graph.
    """
    model.graph.eliminate_dead_code()
    model.recompile()
    conv_node = None
    bn_node = None
    getitem_node = None
    for n in model.graph.nodes:
        if n.target == torch.ops.aten.conv2d.default:
            conv_node = n
        if n.target == torch.ops.aten._native_batch_norm_legit.default:
            bn_node = n
        if n.target == operator.getitem:
            getitem_node = n
    assert conv_node is not None, "bad test setup"
    return (conv_node, bn_node, getitem_node)


class ConvBnInt32WeightQuantizer(Quantizer):
    """
    Dummy quantizer that annotates conv bn in such a way that the weights
    are quantized per channel to int32.
    """

    def annotate(self, model: torch.fx.GraphModule) -> torch.fx.GraphModule:
        conv_node, _, getitem_node = _get_conv_bn_getitem_nodes(model)
        act_qspec = QuantizationSpec(
            dtype=torch.uint8,
            quant_min=0,
            quant_max=255,
            qscheme=torch.per_tensor_affine,
            observer_or_fake_quant_ctr=default_fake_quant,
        )
        weight_qspec = QuantizationSpec(
            dtype=torch.int32,
            quant_min=0,
            quant_max=2**31 - 1,
            qscheme=torch.per_channel_affine,
            observer_or_fake_quant_ctr=FusedMovingAvgObsFakeQuantize.with_args(
                observer=MovingAveragePerChannelMinMaxObserver,
            ),
        )
        conv_node.meta["quantization_annotation"] = QuantizationAnnotation(
            input_qspec_map={
                conv_node.args[0]: act_qspec,
                conv_node.args[1]: weight_qspec,
            },
            _annotated=True,
        )
        getitem_node.meta["quantization_annotation"] = QuantizationAnnotation(
            output_qspec=act_qspec,
            _annotated=True,
        )
        return model

    def validate(self, model: torch.fx.GraphModule):
        pass


class ConvBnDerivedBiasQuantizer(Quantizer):
    """
    Dummy quantizer that annotates conv bn in such a way that the bias qparams are
    derived from the conv input activation and weight qparams.
    """

    def _derive_bias_qparams_from_act_and_weight_qparams(self, obs_or_fqs):
        act_scale, _ = obs_or_fqs[0].calculate_qparams()
        weight_scale, _ = obs_or_fqs[1].calculate_qparams()
        bias_scale = torch.tensor([act_scale * weight_scale], dtype=torch.float32)
        bias_zero_point = torch.tensor([0], dtype=torch.int32)
        return bias_scale, bias_zero_point

    def annotate(self, model: torch.fx.GraphModule) -> torch.fx.GraphModule:
        conv_node, _, getitem_node = _get_conv_bn_getitem_nodes(model)
        act_and_weight_qspec = QuantizationSpec(
            dtype=torch.uint8,
            quant_min=0,
            quant_max=255,
            qscheme=torch.per_tensor_affine,
            observer_or_fake_quant_ctr=default_fake_quant,
        )
        bias_qspec = DerivedQuantizationSpec(
            derived_from=[
                (conv_node.args[0], conv_node),
                (conv_node.args[1], conv_node),
            ],
            derive_qparams_fn=self._derive_bias_qparams_from_act_and_weight_qparams,
            dtype=torch.int32,
            quant_min=-(2**31),
            quant_max=2**31 - 1,
            qscheme=torch.per_tensor_affine,
        )
        conv_node.meta["quantization_annotation"] = QuantizationAnnotation(
            input_qspec_map={
                conv_node.args[0]: act_and_weight_qspec,
                conv_node.args[1]: act_and_weight_qspec,
                conv_node.args[2]: bias_qspec,
            },
            _annotated=True,
        )
        getitem_node.meta["quantization_annotation"] = QuantizationAnnotation(
            output_qspec=act_and_weight_qspec,
            _annotated=True,
        )
        return model

    def validate(self, model: torch.fx.GraphModule):
        pass


@skipIfNoQNNPACK
class TestQuantizePT2EQATModels(PT2EQATTestCase):
    @skip_if_no_torchvision
    @skipIfNoQNNPACK
    def test_qat_resnet18(self):
        import torchvision

        with override_quantized_engine("qnnpack"):
            example_inputs = (torch.randn(1, 3, 224, 224),)
            m = torchvision.models.resnet18()
            self._verify_symmetric_xnnpack_qat_numerics(m, example_inputs)

    @skip_if_no_torchvision
    @skipIfNoQNNPACK
    def test_qat_mobilenet_v2(self):
        import torchvision

        with override_quantized_engine("qnnpack"):
            example_inputs = (torch.randn(1, 3, 224, 224),)
            m = torchvision.models.mobilenet_v2()
            self._verify_symmetric_xnnpack_qat_numerics(m, example_inputs)


class TestQuantizeMixQATAndPTQ(QuantizationTestCase):
    class TwoLinear(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.linear1 = torch.nn.Linear(16, 8, bias=False)
            self.linear2 = torch.nn.Linear(8, 8)

        def forward(self, x):
            return self.linear2(self.linear1(x))

    class QATPTQTestModule(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.conv = torch.nn.Conv2d(3, 16, 3)
            self.linears = TestQuantizeMixQATAndPTQ.TwoLinear()
            self.my_linear = torch.nn.Linear(8, 8)

        def forward(self, x):
            conv_out = self.conv(x)
            permute_out = torch.permute(conv_out, (0, 2, 3, 1))
            linear_out = self.linears(permute_out)
            my_linear_out = self.my_linear(linear_out)
            # Hardtanh doesnt get quantized via xnnpack quantizer in this test
            # because it relies on the propagation rules
            # Need to fix this
            return torch.nn.functional.hardtanh(my_linear_out)

    def _prepare_qat_linears(self, model):
        for name, child in model.named_children():
            if isinstance(child, (torch.nn.Linear, TestQuantizeMixQATAndPTQ.TwoLinear)):
                if isinstance(child, torch.nn.Linear):
                    in_channels = child.weight.size(1)
                else:
                    in_channels = child.linear1.weight.size(1)

                example_input = (torch.rand((1, in_channels)),)
                traced_child = capture_pre_autograd_graph(child, example_input)
                quantizer = XNNPACKQuantizer()
                quantization_config = get_symmetric_quantization_config(
                    is_per_channel=True, is_qat=True
                )
                quantizer.set_global(quantization_config)
                traced_child_prepared = prepare_qat_pt2e(traced_child, quantizer)
                setattr(model, name, traced_child_prepared)
            else:
                self._prepare_qat_linears(child)

    def _convert_qat_linears(self, model):
        for name, child in model.named_children():
            if isinstance(child, torch.fx.GraphModule):
                torch.ao.quantization.move_exported_model_to_eval(child)
                converted_child = convert_pt2e(child, fold_quantize=True)
                setattr(model, name, converted_child)
            else:
                self._convert_qat_linears(child)

    def test_mixing_qat_ptq(self):
        example_inputs = (torch.randn(2, 3, 4, 4),)
        model = TestQuantizeMixQATAndPTQ.QATPTQTestModule()

        self._prepare_qat_linears(model)

        after_prepare_result_pt2e = model(*example_inputs)
        # must be fixed model.eval()
        self._convert_qat_linears(model)
        quant_result_pt2e = model(*example_inputs)

        model_pt2e = capture_pre_autograd_graph(
            model,
            example_inputs,
        )

        quantizer = XNNPACKQuantizer()
        quantizer.set_module_type(torch.nn.Linear, None)
        quantization_config = get_symmetric_quantization_config()
        quantizer.set_global(quantization_config)
        model_pt2e = prepare_pt2e(model_pt2e, quantizer)
        after_prepare_result_pt2e = model_pt2e(*example_inputs)
        model_pt2e = convert_pt2e(model_pt2e)
        quant_result_pt2e = model_pt2e(*example_inputs)

        exported_model = torch.export.export(model_pt2e, example_inputs)

        node_occurrence = {
            # conv2d: 1 for act, 1 for weight, 1 for output
            # 3 x linear: 1 for act, 1 for output
            ns.call_function(
                torch.ops.quantized_decomposed.quantize_per_tensor.default
            ): 9,
            ns.call_function(
                torch.ops.quantized_decomposed.dequantize_per_tensor.default
            ): 9,
            ns.call_function(
                torch.ops.quantized_decomposed.dequantize_per_channel.default
            ): 3,
            # There needs to be one for hardtanh
        }
        self.checkGraphModuleNodes(
            exported_model.graph_module, expected_node_occurrence=node_occurrence
        )
