// Independently implemented integer SDR conversion candidate. No third-party
// implementation embedded. Unscaled BT.601 limited I420; black even-size pad.
// Every thread writes a complete 2x2 block, so no intermediate frame or atomics.
extern "C" __global__ void sdr_to_i420(const unsigned char* src,
    unsigned char* dst, unsigned width, unsigned height,
    unsigned long long pitch, unsigned channels, unsigned red, unsigned blue) {
    unsigned ow=(width+1)&~1u, oh=(height+1)&~1u;
    unsigned cx=blockIdx.x*blockDim.x+threadIdx.x;
    unsigned cy=blockIdx.y*blockDim.y+threadIdx.y;
    if (cx>=ow/2 || cy>=oh/2) return;
    unsigned sum_r=0,sum_g=0,sum_b=0;
    for (unsigned dy=0;dy<2;++dy) for (unsigned dx=0;dx<2;++dx) {
        unsigned x=2*cx+dx,y=2*cy+dy;
        unsigned r=0,g=0,b=0;
        if (x<width && y<height) {
            const unsigned char* p=src+pitch*y+channels*x;
            r=p[red];g=p[1];b=p[blue];
        }
        sum_r+=r;sum_g+=g;sum_b+=b;
        dst[y*ow+x]=(unsigned char)(16+((8414*r+16519*g+3208*b)>>15));
    }
    // Average RGB channels with truncation BEFORE chroma conversion. Combining
    // these into one rounded matrix changes production bytes, even for white.
    int r=sum_r>>2,g=sum_g>>2,b=sum_b>>2;
    unsigned uv=cy*(ow/2)+cx, plane=ow*oh;
    dst[plane+uv]=(unsigned char)((128*32768-4865*r-9528*g+14392*b)>>15);
    dst[plane+plane/4+uv]=(unsigned char)((128*32768+14392*r-12061*g-2332*b)>>15);
}
