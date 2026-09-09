"""Tiny CPU math checks, optional Torch build-environment test (no model files)."""
import importlib.util
import sys
import types
import unittest
from unittest import mock

from dlss5tool import depth_acceleration


@unittest.skipUnless(importlib.util.find_spec('torch'), 'Torch is intentionally absent from the base app')
class DepthAttentionTests(unittest.TestCase):
    def setUp(self):
        import torch
        self.torch = torch
        torch.manual_seed(71)
        class Attention(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.num_heads = 4
                self.scale = 8 ** -0.5
                self.qkv = torch.nn.Linear(32, 96)
                self.proj = torch.nn.Linear(32, 32)
                self.proj_drop = torch.nn.Dropout(0.4)
            def forward(self, x):
                b, n, c = x.shape
                q, k, v = self.qkv(x).reshape(b, n, 3, 4, 8).permute(2, 0, 3, 1, 4)
                attn = ((q * self.scale) @ k.transpose(-2, -1)).softmax(-1)
                return self.proj_drop(self.proj((attn @ v).transpose(1, 2).reshape(b, n, c)))
        self.Attention = Attention

    def test_matches_original_scaling_and_disables_eval_dropout(self):
        torch = self.torch
        module = self.Attention().eval()
        tensor = torch.randn(2, 65, 32)
        with torch.inference_mode():
            expected = module(tensor)
            actual = depth_acceleration.sdpa_forward(module, tensor)
            torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-6)
            torch.testing.assert_close(actual, depth_acceleration.sdpa_forward(module, tensor), rtol=0, atol=0)

    def test_rejects_training_and_nested_bias(self):
        module = self.Attention()
        tensor = self.torch.randn(1, 5, 32)
        with self.assertRaises(ValueError):
            depth_acceleration.sdpa_forward(module, tensor)
        module.eval()
        with self.assertRaises(ValueError):
            depth_acceleration.sdpa_forward(module, tensor, object())

    def test_supported_layer_counts_and_state_dict_preserved(self):
        fake = types.ModuleType('depth_anything_v2.dinov2_layers.attention')
        fake.Attention = self.Attention
        with mock.patch.dict(sys.modules, {'depth_anything_v2.dinov2_layers.attention': fake}):
            for encoder, count in (('vits', 12), ('vitb', 12), ('vitl', 24)):
                model = self.torch.nn.Sequential(*(self.Attention() for _ in range(count))).eval()
                before = {key: value.clone() for key, value in model.state_dict().items()}
                self.assertEqual(depth_acceleration.enable_sdpa(model, encoder), count)
                for key, value in model.state_dict().items():
                    self.torch.testing.assert_close(value, before[key], rtol=0, atol=0)
            bad = self.torch.nn.Sequential(self.Attention()).eval()
            with self.assertRaises(ValueError):
                depth_acceleration.enable_sdpa(bad, 'vitl')


if __name__ == '__main__':
    unittest.main()
