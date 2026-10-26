// Independent production-compatible BT.2020 limited P010 to RGBA16F.
__device__ float chroma(const unsigned char* source,unsigned long long pitch,
    int x,int y,int channel,unsigned width,unsigned height) {
    x=max(0,min(x,int(width/2)-1)); y=max(0,min(y,int(height/2)-1));
    unsigned value=((const unsigned short*)(source+y*pitch))[2*x+channel]>>6;
    return float(value);
}
__device__ float upsample(const unsigned char* source,unsigned long long pitch,
    int x,int y,int channel,unsigned width,unsigned height) {
    int left=x/2,top=(y-1)/2;
    if(y==0) top=-1;
    float fx=(x&1) ? .5f : 0.f,fy=(y&1) ? .25f : .75f;
    float a=__fadd_rn(__fmul_rn(chroma(source,pitch,left,top,channel,width,height),1.f-fx),
                     __fmul_rn(chroma(source,pitch,left+1,top,channel,width,height),fx));
    float b=__fadd_rn(__fmul_rn(chroma(source,pitch,left,top+1,channel,width,height),1.f-fx),
                     __fmul_rn(chroma(source,pitch,left+1,top+1,channel,width,height),fx));
    // Production zimg chooses WORD resampling for the admitted geometry.
    // Quantize after EACH axis, before conversion to normalized FP32.
    a=floorf(a+.5f); b=floorf(b+.5f);
    float code=floorf(__fadd_rn(__fmul_rn(a,1.f-fy),__fmul_rn(b,fy))+.5f);
    return __fmul_rn(code-512.f,1.f/896.f);
}
__device__ unsigned short quantized_half(float value) {
    unsigned code=__float2uint_rn(__fmul_rn(fminf(fmaxf(value,0.f),1.f),65535.f));
    float normalized=__fdiv_rn(float(code),65535.f);
    unsigned short half;
    asm("cvt.rn.f16.f32 %0,%1;" : "=h"(half) : "f"(normalized));
    return half;
}
extern "C" __global__ void p010_to_rgba(const unsigned char* y_plane,
    const unsigned char* uv_plane,unsigned char* output,
    unsigned long long y_pitch,unsigned long long uv_pitch,unsigned long long output_pitch,
    unsigned width,unsigned height) {
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=width || y>=height) return;
    unsigned code=((const unsigned short*)(y_plane+y*y_pitch))[x]>>6;
    float lum=__fmul_rn(float(int(code)-64),1.f/876.f);
    float u=upsample(uv_plane,uv_pitch,x,y,0,width,height);
    float v=upsample(uv_plane,uv_pitch,x,y,1,width,height);
    float r=__fmaf_rn(v,float(2*(1-.2627)),lum);
    float g=__fmaf_rn(v,float(-2*.2627*(1-.2627)/.678),
                     __fmaf_rn(u,float(-2*.0593*(1-.0593)/.678),lum));
    float b=__fmaf_rn(u,float(2*(1-.0593)),lum);
    unsigned short* dst=(unsigned short*)(output+y*output_pitch)+4*x;
    dst[0]=quantized_half(r);dst[1]=quantized_half(g);dst[2]=quantized_half(b);dst[3]=0x3c00;
}
