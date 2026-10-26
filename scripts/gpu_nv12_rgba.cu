// Independent integer NV12 -> RGBA implementation. Compatibility target is the
// application's planar 8-bit YUV -> BGR conversion, with separate signed
// fixed-point channel products and truncation. No upstream source embedded.
__device__ unsigned char saturate_byte(int value) {
    return (unsigned char)(value < 0 ? 0 : value > 255 ? 255 : value);
}
extern "C" __global__ void nv12_to_rgba(const unsigned char* y_plane,
    const unsigned char* uv_plane, unsigned char* output,
    unsigned long long y_pitch, unsigned long long uv_pitch,
    unsigned long long output_pitch, unsigned width, unsigned height,
    int y_coeff, int vr_coeff, int ub_coeff, int ug_coeff, int vg_coeff,
    int y_offset) {
    unsigned x = blockIdx.x*blockDim.x + threadIdx.x;
    unsigned y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x >= width || y >= height) return;
    unsigned long long uv = (y/2)*uv_pitch + (x/2)*2;
    int lum = ((int(y_plane[y*y_pitch+x])*8-y_offset)*y_coeff) >> 16;
    int u = int(uv_plane[uv])*8-1024;
    int v = int(uv_plane[uv+1])*8-1024;
    unsigned char* pixel = output+y*output_pitch+x*4;
    pixel[0] = saturate_byte(lum+((v*vr_coeff)>>16));
    pixel[1] = saturate_byte(lum+((u*ug_coeff)>>16)+((v*vg_coeff)>>16));
    pixel[2] = saturate_byte(lum+((u*ub_coeff)>>16));
    pixel[3] = 255;
}
