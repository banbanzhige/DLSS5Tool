// Exact legacy VSR wire conversion, including its custom half rounding.
__device__ float legacy_half(unsigned h) {
    unsigned sign=(h&0x8000u)<<16, mantissa=h&1023u;
    int exponent=int((h>>10)&31u);
    if (!exponent) {
        if (!mantissa) return __uint_as_float(sign);
        exponent=1;
        while (!(mantissa&1024u)) { mantissa<<=1; --exponent; }
        return __uint_as_float(sign|(unsigned(exponent+112)<<23)|((mantissa&1023u)<<13));
    }
    return __uint_as_float(sign|((exponent==31 ? 255u : unsigned(exponent+112))<<23)|(mantissa<<13));
}
__device__ unsigned legacy_encode(float value) {
    unsigned bits=__float_as_uint(value), sign=(bits>>16)&0x8000u, mantissa=bits&0x7fffffu;
    int exponent=int((bits>>23)&255u)-112;
    if (exponent<=0) {
        if (exponent < -10) return sign;
        mantissa=(mantissa|0x800000u)>>(1-exponent);
        return sign|((mantissa+0x1000u)>>13);
    }
    if (exponent>=31) return sign|0x7c00u;
    return sign|(unsigned(exponent)<<10)|((mantissa+0x1000u)>>13);
}
__device__ unsigned channel(unsigned h) {
    float value=fminf(fmaxf(legacy_half(h),0.f),1.f);
    return unsigned(__fadd_rn(__fmul_rn(value,1023.f),.5f));
}
extern "C" __global__ void vsr_pack(const unsigned char* input,unsigned char* output,
    unsigned width,unsigned height,unsigned long long input_pitch,unsigned long long output_pitch) {
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if (x>=width || y>=height) return;
    const unsigned short* pixel=(const unsigned short*)(input+y*input_pitch)+4*x;
    ((unsigned*)(output+y*output_pitch))[x]=channel(pixel[0])|(channel(pixel[1])<<10)|
        (channel(pixel[2])<<20)|(3u<<30);
}
extern "C" __global__ void vsr_unpack(const unsigned char* input,unsigned char* output,
    unsigned width,unsigned height,unsigned long long input_pitch,unsigned long long output_pitch) {
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if (x>=width || y>=height) return;
    unsigned pixel=((const unsigned*)(input+y*input_pitch))[x];
    unsigned short* dst=(unsigned short*)(output+y*output_pitch)+4*x;
    for(int c=0;c<3;++c) dst[c]=(unsigned short)legacy_encode(__fdiv_rn(float((pixel>>(10*c))&1023u),1023.f));
    dst[3]=0x3c00;
}
extern "C" __global__ void validate_half(const unsigned char* input,unsigned width,unsigned height,
    unsigned long long pitch,unsigned* invalid) {
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=width || y>=height) return;
    const unsigned short* pixel=(const unsigned short*)(input+y*pitch)+4*x;
    for(int c=0;c<4;++c) {
        float value=legacy_half(pixel[c]);
        if(!isfinite(value) || value<0.f || value>1.f) atomicOr(invalid,1u);
    }
}
