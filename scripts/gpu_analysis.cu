// Exact EOTF and byte boundaries from the frozen CPU analysis contract.
template<class T> __device__ unsigned code(T bits,const T* thresholds,const T* corrections,
    const unsigned char* values,unsigned count) {
    unsigned a=0,b=count;
    while(a<b) {unsigned mid=(a+b)/2;if(corrections[mid]<bits) a=mid+1;else b=mid;}
    if(a<count && corrections[a]==bits) return values[a];
    unsigned lo=0,hi=256;
    while(lo+1<hi) {unsigned mid=(lo+hi)/2;if(bits>=thresholds[mid]) lo=mid;else hi=mid;}
    return lo;
}
extern "C" __global__ void analysis_half(const unsigned char* source,unsigned char* output,
    unsigned width,unsigned height,unsigned long long source_pitch,unsigned long long output_pitch,
    const float* pq,const double* hlg,const unsigned* threshold32,const unsigned long long* threshold64,
    const float* matrix,unsigned use_hlg,unsigned bt2020,unsigned* invalid,
    const unsigned* keys32,const unsigned char* values32,unsigned count32,
    const unsigned long long* keys64,const unsigned char* values64,unsigned count64) {
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=width || y>=height) return;
    const unsigned short* input=(const unsigned short*)(source+y*source_pitch)+4*x;
    unsigned char* dst=output+y*output_pitch+4*x;
    for(int c=0;c<3;++c) if((input[c]&0x7c00u)==0x7c00u) atomicOr(invalid,1u);
    for(int c=0;c<3;++c) {
        if(use_hlg) {
            double r=hlg[input[0]],g=hlg[input[1]],b=hlg[input[2]];
            double value=bt2020 ? __dadd_rn(__dadd_rn(__dmul_rn(r,double(matrix[3*c])),
                __dmul_rn(g,double(matrix[3*c+1]))),__dmul_rn(b,double(matrix[3*c+2]))) : hlg[input[c]];
            dst[c]=(unsigned char)code((unsigned long long)__double_as_longlong(fmax(value,0.0)),threshold64,
                keys64,values64,count64);
        } else {
            float r=pq[input[0]],g=pq[input[1]],b=pq[input[2]];
            float value=bt2020 ? __fadd_rn(__fadd_rn(__fmul_rn(r,matrix[3*c]),
                __fmul_rn(g,matrix[3*c+1])),__fmul_rn(b,matrix[3*c+2])) : pq[input[c]];
            dst[c]=(unsigned char)code(__float_as_uint(fmaxf(value,0.f)),threshold32,keys32,values32,count32);
        }
    }
    dst[3]=255;
}
extern "C" __global__ void analysis_gray(const unsigned char* source,unsigned char* output,
    unsigned width,unsigned height,unsigned output_width,unsigned output_height,
    unsigned long long source_pitch,unsigned long long output_pitch) {
    unsigned x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=output_width || y>=output_height) return;
    const unsigned char* input=source+min(y,height-1)*source_pitch+4*min(x,width-1);
    output[y*output_pitch+x]=(unsigned char)((9798u*input[0]+19235u*input[1]+3735u*input[2]+16384u)>>15);
}
