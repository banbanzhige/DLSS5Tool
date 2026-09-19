// Independent fixed-point BT2020 conversion. Matches the tested unscaled
// FFmpeg 7.1.1 gbrap16 -> swscale bicubic -> P010 contract; not linear-light mix.
__device__ float half_value(unsigned short h) {
    unsigned sign=(h&0x8000u)<<16, exponent=(h>>10)&31u, mantissa=h&1023u;
    if (!exponent) return (h&0x8000u ? -1.f : 1.f)*float(mantissa)*(1.f/16777216.f);
    return __uint_as_float(sign|((exponent==31 ? 255u : exponent+112u)<<23)|(mantissa<<13));
}
__device__ int rgb_sample(const unsigned char* src,unsigned w,unsigned h,
    unsigned long long pitch,int x,int y,int channel,unsigned floating) {
    if (x<0 || y<0 || x>=w || y>=h) return 0;
    const unsigned char* row=src+pitch*y;
    float value=floating ? ((const float*)row)[x*4+channel] : half_value(((const unsigned short*)row)[x*4+channel]);
    if (isnan(value)) value=0.f;
    return __float2int_rn(fminf(fmaxf(value,0.f),1.f)*65535.f);
}
extern "C" __global__ void hdr_validate(const unsigned char* src,unsigned width,unsigned height,
    unsigned long long pitch,unsigned floating,unsigned* invalid) {
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=width || y>=height) return;
    const unsigned char* row=src+pitch*y;
    for(int c=0;c<4;++c) {
        float value=floating ? ((const float*)row)[x*4+c] : half_value(((const unsigned short*)row)[x*4+c]);
        if(!isfinite(value) || value<0.f || value>1.f) atomicOr(invalid,1u);
    }
}
__device__ int chroma_value(const unsigned char* src,unsigned w,unsigned h,
    unsigned long long pitch,int x,int y,unsigned floating,int v) {
    int r=rgb_sample(src,w,h,pitch,x,y,0,floating);
    int g=rgb_sample(src,w,h,pitch,x,y,1,floating);
    int b=rgb_sample(src,w,h,pitch,x,y,2,floating);
    int sum=v ? 14392*r-13235*g-1158*b : -4019*r-10373*g+14392*b;
    return ((sum+1073741824+16384)>>15)&65535;
}
extern "C" __global__ void hdr_horizontal(const unsigned char* src,unsigned short* dst,
    int* rows,const int* indices,const int* weights,unsigned width,unsigned height,
    unsigned long long pitch,unsigned floating) {
    unsigned ow=(width+1)&~1u,oh=(height+1)&~1u;
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if (y>=oh || x>=ow) return;
    int r=rgb_sample(src,width,height,pitch,x,y,0,floating);
    int g=rgb_sample(src,width,height,pitch,x,y,1,floating);
    int b=rgb_sample(src,width,height,pitch,x,y,2,floating);
    int luma=(7393*r+19080*g+1669*b+134217728+16384)>>15;
    dst[y*ow+x]=(unsigned short)(((luma/2+16)>>5)<<6);
    if (x>=ow/2) return;
    for (int v=0;v<2;++v) {
        int sum=0;
        for(int tap=0;tap<8;++tap)
            sum+=chroma_value(src,width,height,pitch,indices[x*8+tap],y,floating,v)*weights[x*8+tap];
        rows[(y*(ow/2)+x)*2+v]=min(sum>>15,32767);
    }
}
extern "C" __global__ void hdr_vertical(const int* rows,unsigned short* dst,
    const int* indices,const int* weights,unsigned width,unsigned height) {
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if (x>=width/2 || y>=height/2) return;
    for (int v=0;v<2;++v) {
        int sum=65536;
        for(int tap=0;tap<8;++tap)
            sum+=rows[(indices[y*8+tap]*(width/2)+x)*2+v]*weights[y*8+tap];
        dst[width*height+y*width+2*x+v]=(unsigned short)(min(max(sum>>17,0),1023)<<6);
    }
}
