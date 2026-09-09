"""Opt-in SDPA adapter for the packaged Depth Anything V2 architecture.

No third-party source edits and no xFormers dependency. This adapter is for
ordinary eval tensors only; nested attention is intentionally not supported.
"""
import types


def sdpa_forward(self, x, attn_bias=None):
    import torch.nn.functional as F
    if self.training or attn_bias is not None:
        raise ValueError('Depth SDPA adapter only supports non-nested inference')
    batch, tokens, channels = x.shape
    qkv = self.qkv(x).reshape(batch, tokens, 3, self.num_heads,
                              channels // self.num_heads).permute(2, 0, 3, 1, 4)
    q, k, v = qkv.unbind(0)
    values = F.scaled_dot_product_attention(q, k, v, dropout_p=0.0, scale=self.scale)
    values = values.transpose(1, 2).reshape(batch, tokens, channels)
    return self.proj_drop(self.proj(values))


def enable_sdpa(model, encoder):
    from depth_anything_v2.dinov2_layers.attention import Attention
    expected = {'vits': 12, 'vitb': 12, 'vitl': 24}[encoder]
    modules = [module for module in model.modules() if isinstance(module, Attention)]
    if len(modules) != expected or model.training:
        raise ValueError('Incompatible depth attention architecture')
    for module in modules:
        module.forward = types.MethodType(sdpa_forward, module)
    return len(modules)
