# Owner(s): ["module: inductor"]
from typing import NamedTuple

import torch
from torch._dynamo.testing import load_test_module
from torch._inductor import config
from torch.testing._internal.common_utils import (
    IS_MACOS,
    slowTest,
    TEST_WITH_ASAN,
    TEST_WITH_ROCM,
    TestCase as TorchTestCase,
)
from torch.testing._internal.inductor_utils import (
    HAS_CPU,
    HAS_CUDA,
    run_and_get_cpp_code,
    TestFailure,
)

test_cpu_repro = load_test_module(__file__, "inductor.test_cpu_repro")
test_foreach = load_test_module(__file__, "inductor.test_foreach")
test_mkldnn_pattern_matcher = load_test_module(
    __file__, "inductor.test_mkldnn_pattern_matcher"
)
test_pattern_matcher = load_test_module(__file__, "inductor.test_pattern_matcher")
test_select_algorithm = load_test_module(__file__, "inductor.test_select_algorithm")
test_torchinductor = load_test_module(__file__, "inductor.test_torchinductor")
test_torchinductor_dynamic_shapes = load_test_module(
    __file__, "inductor.test_torchinductor_dynamic_shapes"
)


RUN_CPU = HAS_CPU and not torch.backends.mps.is_available() and not IS_MACOS
RUN_CUDA = HAS_CUDA and not TEST_WITH_ASAN and not TEST_WITH_ROCM


class CppWrapperTemplate:
    pass


class CudaWrapperTemplate:
    pass


class TestCppWrapper(TorchTestCase):
    device = "cpu"


class DynamicShapesCppWrapperCpuTests(TorchTestCase):
    device = "cpu"


class TestCudaWrapper(TorchTestCase):
    device = "cuda"


class DynamicShapesCudaWrapperCudaTests(TorchTestCase):
    device = "cuda"


test_failures_cpp_wrapper = {
    # conv2d will fallback for dynamic shapes; the fallback path is not yet supported
    "test_conv2d_unary_cpu_dynamic_shapes": TestFailure(("cpp_wrapper",), is_skip=True),
    "test_conv2d_binary_inplace_fusion_failed_cpu_dynamic_shapes": TestFailure(
        ("cpp_wrapper",), is_skip=True
    ),
    "test_conv2d_binary_inplace_fusion_pass_cpu_dynamic_shapes": TestFailure(
        ("cpp_wrapper",), is_skip=True
    ),
    # aten._native_multi_head_attention.default is not yet supported for dynamic shapes
    "test_multihead_attention_cpu_dynamic_shapes": TestFailure(
        ("cpp_wrapper",), is_skip=True
    ),
}

test_failures_cuda_wrapper = {
    "test_mm_plus_mm2_dynamic_shapes": TestFailure(("cuda_wrapper",), is_skip=True),
}


def make_test_case(name, device, tests, condition=True, slow=False, func_inputs=None):
    test_name = f"{name}_{device}" if device else name

    func = getattr(tests, test_name)
    assert callable(func), "not a callable"
    func = slowTest(func) if slow else func

    @config.patch(cpp_wrapper=True, search_autotune_cache=False)
    def fn(self):
        tests.setUpClass()
        tests.setUp()
        try:
            _, code = run_and_get_cpp_code(func, *func_inputs if func_inputs else [])
            self.assertEqual("CppWrapperCodeCache" in code, True)
        finally:
            tests.tearDown()
            tests.tearDownClass()

    fn.__name__ = test_name
    import copy

    fn.__dict__ = copy.deepcopy(func.__dict__)
    if condition:
        setattr(
            CppWrapperTemplate if device == "cpu" else CudaWrapperTemplate,
            test_name,
            fn,
        )


if RUN_CPU:

    class BaseTest(NamedTuple):
        name: str
        device: str = "cpu"
        tests: TorchTestCase = test_torchinductor.CpuTests()
        condition: bool = True
        slow: bool = False
        func_inputs: list = None

    for item in [
        BaseTest("test_as_strided"),  # buffer reuse
        BaseTest("test_bitwise"),  # int32
        BaseTest("test_bmm1"),
        BaseTest("test_bmm2"),
        BaseTest("test_cat"),  # alias
        BaseTest(
            "test_conv2d_binary_inplace_fusion_failed",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
            func_inputs=[
                ["op_convolution_pointwise_binary.call"],
                ["op_convolution_pointwise_binary_.call"],
            ],
        ),
        BaseTest(
            "test_conv2d_binary_inplace_fusion_pass",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
            func_inputs=[
                ["op_convolution_pointwise_binary_.call"],
                ["op_convolution_pointwise_binary.call"],
            ],
        ),
        BaseTest(
            "test_conv2d_unary",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
            slow=True,
        ),
        BaseTest("test_conv_transpose2d_packed", "cpu", test_cpu_repro.CPUReproTests()),
        BaseTest("test_custom_op"),
        BaseTest("test_dtype_sympy_expr"),
        BaseTest("test_embedding_bag"),  # test default FallbackKernel
        BaseTest("test_index_put_deterministic_fallback"),
        BaseTest("test_adding_tensor_offsets"),
        BaseTest("test_int_div", "", test_cpu_repro.CPUReproTests()),
        BaseTest("test_linear1"),
        BaseTest("test_linear2"),
        BaseTest(
            "test_linear_binary",
            "",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            torch.backends.mkldnn.is_available()
            and torch.ops.mkldnn._is_mkldnn_bf16_supported(),
        ),
        BaseTest("test_linear_packed", "", test_cpu_repro.CPUReproTests()),
        BaseTest(
            "test_lstm_packed_change_input_sizes",
            "cpu",
            test_cpu_repro.CPUReproTests(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest("test_mm_views"),
        BaseTest("test_multihead_attention", "cpu", test_cpu_repro.CPUReproTests()),
        BaseTest("test_multi_threading"),
        BaseTest("test_profiler_mark_wrapper_call"),
        BaseTest(
            "test_qconv2d",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest(
            "test_qconv2d_relu",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest(
            "test_qconv2d_add",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest(
            "test_qconv2d_add_relu",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest(
            "test_qconv2d_dequant_promotion",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest(
            "test_qconv2d_maxpool2d_linear_dynamic",
            "cpu",
            test_mkldnn_pattern_matcher.TestDynamicPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
            func_inputs=[
                [
                    "op_qconv2d_pointwise.call",
                    "op_quantized_max_pool2d_.call",
                    "op_qlinear_pointwise.call",
                ]
            ],
        ),
        BaseTest(
            "test_qlinear",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest(
            "test_qlinear_relu",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest(
            "test_qlinear_dequant_promotion",
            "cpu",
            test_mkldnn_pattern_matcher.TestPatternMatcher(),
            condition=torch.backends.mkldnn.is_available(),
        ),
        BaseTest("test_randint"),
        BaseTest("test_randn_with_dtype_and_device"),
        BaseTest("test_reduction1"),  # Reduction
        BaseTest("test_relu"),  # multiple inputs
        BaseTest("test_repeat_interleave", "", test_cpu_repro.CPUReproTests()),
        BaseTest("test_scalar_input"),
        BaseTest("test_scaled_dot_product_attention"),
        BaseTest("test_scatter1"),
        BaseTest("test_scatter2"),
        BaseTest("test_scatter3"),
        BaseTest("test_scatter4"),
        BaseTest("test_scatter5"),
        BaseTest("test_scatter6"),
        BaseTest("test_scatter_reduce1"),
        BaseTest("test_scatter_reduce2"),
        BaseTest("test_scatter_reduce3"),
        BaseTest("test_silu"),  # single input, single output
        BaseTest("test_sort"),
        BaseTest("test_sum_dtype"),  # float64
        BaseTest("test_sum_int"),  # bool, int64, int8, uint8
        BaseTest("test_tensor2"),  # constant input
        BaseTest("test_transpose"),  # multiple outputs, buffer clear
        BaseTest("test_view_as_complex"),
        BaseTest("test_view_as_real"),
    ]:
        make_test_case(
            item.name,
            item.device,
            item.tests,
            item.condition,
            item.slow,
            item.func_inputs,
        )

    test_torchinductor.copy_tests(CppWrapperTemplate, TestCppWrapper, "cpp_wrapper")

    DynamicShapesCppWrapperTemplate = (
        test_torchinductor_dynamic_shapes.make_dynamic_cls(CppWrapperTemplate)
    )

    test_torchinductor.copy_tests(
        DynamicShapesCppWrapperTemplate,
        DynamicShapesCppWrapperCpuTests,
        "cpp_wrapper",
        test_failures_cpp_wrapper,
        xfail_prop="_expected_failure_dynamic_wrapper",
    )

if RUN_CUDA:

    class BaseTest(NamedTuple):
        name: str
        device: str = "cuda"
        tests: TorchTestCase = test_torchinductor.CudaTests()

    # Maintain two separate test lists for cuda and cpp for now
    for item in [
        BaseTest("test_as_strided"),  # buffer reuse
        BaseTest("test_batch_norm_2d_2"),
        BaseTest("test_bitwise"),  # int32
        BaseTest("test_bmm1"),
        BaseTest("test_bmm2"),
        BaseTest("test_cat"),  # alias
        BaseTest("test_convolution1"),
        BaseTest("test_conv_backward"),
        BaseTest("test_custom_op"),
        BaseTest("test_embedding_bag"),  # test default FallbackKernel
        BaseTest("test_index_put_deterministic_fallback"),
        BaseTest("test_adding_tensor_offsets"),
        BaseTest("test_index_tensor"),
        BaseTest("test_linear1"),
        BaseTest("test_linear2"),
        BaseTest("test_mm_views"),
        BaseTest("test_multi_device"),
        BaseTest("test_multi_threading"),
        BaseTest("test_profiler_mark_wrapper_call"),
        BaseTest("test_reduction1"),  # Reduction
        BaseTest("test_relu"),  # multiple inputs
        BaseTest("test_repeat_interleave_2"),
        BaseTest("test_scalar_input"),
        BaseTest("test_scaled_dot_product_attention"),
        BaseTest("test_scaled_dot_product_efficient_attention"),
        BaseTest("test_sort"),
        BaseTest("test_silu"),  # single input, single output
        BaseTest("test_sum_dtype"),  # float64
        BaseTest("test_sum_int"),  # bool, int64, int8, uint8
        BaseTest("test_transpose"),  # multiple outputs, buffer clear
        BaseTest(
            "test_foreach_cpp_wrapper",
            device=None,
            tests=test_foreach.ForeachTests(),
        ),  # test foreach
        BaseTest(
            "test_cat_slice_cat",
            device=None,
            tests=test_pattern_matcher.TestPatternMatcher(),
        ),
        BaseTest(
            "test_addmm",
            device=None,
            tests=test_select_algorithm.TestSelectAlgorithm(),
        ),
        BaseTest(
            "test_linear_relu",
            device=None,
            tests=test_select_algorithm.TestSelectAlgorithm(),
        ),
        # TODO: Re-enable this test after fixing cuda wrapper for conv Triton templates with dynamic shapes.
        # This test is unstable: it succeeds when an ATEN kernel is used, and fails when a Triton kernel is used.
        # Currently it passes on CI (an ATEN kernel is chosen) and fails locally (a Triton kernel is chosen).
        # Ideally, it should succeed for whatever kernels.
        # BaseTest(
        #     "test_convolution1",
        #     device=None,
        #     tests=test_select_algorithm.TestSelectAlgorithm(),
        # ),
        BaseTest(
            "test_mm_plus_mm2",
            device=None,
            tests=test_select_algorithm.TestSelectAlgorithm(),
        ),
        BaseTest("test_fft_real_input"),
        BaseTest("test_fft_real_input_real_output"),
    ]:
        make_test_case(item.name, item.device, item.tests)

    test_torchinductor.copy_tests(CudaWrapperTemplate, TestCudaWrapper, "cuda_wrapper")

    DynamicShapesCudaWrapperTemplate = (
        test_torchinductor_dynamic_shapes.make_dynamic_cls(CudaWrapperTemplate)
    )

    test_torchinductor.copy_tests(
        DynamicShapesCudaWrapperTemplate,
        DynamicShapesCudaWrapperCudaTests,
        "cuda_wrapper",
        test_failures_cuda_wrapper,
    )


if __name__ == "__main__":
    from torch.testing._internal.inductor_utils import run_inductor_tests

    run_inductor_tests(skip_asan=True, skip_rocm=True, skip_mac=True)
