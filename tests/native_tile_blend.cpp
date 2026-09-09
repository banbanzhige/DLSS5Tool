#include "../native/host_v2/tile_blend.h"
#include <cassert>
#include <cstdio>

void Axis(unsigned length, unsigned tile) {
    auto regions = tile_blend::Plan(length, tile);
    std::vector<float> sum(length, 0);
    for (const auto &r : regions) {
        assert(r.size == std::min(length,tile));
        assert(r.start + r.size <= length && r.begin < r.end);
        for (unsigned p=r.begin; p<r.end; ++p) sum[p] += r.weights[p-r.begin];
    }
    for (float weight : sum) assert(std::abs(weight-1.0f)<0.00001f);
}

void Identity(unsigned w, unsigned h, unsigned tile) {
    auto xs=tile_blend::Plan(w,tile), ys=tile_blend::Plan(h,tile);
    tile_blend::Rows rows(w,std::min(h,tile));
    unsigned count=0;
    // Signed, >1 values also ensure the accumulator doesn't clamp HDR.
    auto sample=[](unsigned x,unsigned y,unsigned c) { return float(int(x*3+y*7+c)%137-20)/8; };
    for(size_t i=0;i<ys.size();++i) {
        const auto &y=ys[i];
        for(const auto &x:xs)
            rows.Add(x,y,[&](unsigned px,unsigned py,unsigned c) {
                // Deliberately corrupt discarded context: must not leak.
                if (px+x.start<x.begin || px+x.start>=x.end ||
                    py+y.start<y.begin || py+y.start>=y.end) return 10000.0f;
                return sample(px+x.start,py+y.start,c);
            });
        rows.Flush(i+1<ys.size()?ys[i+1].begin:h,[&](unsigned x,unsigned y,unsigned c,float value) {
            assert(std::abs(value-sample(x,y,c))<0.0001f);
            ++count;
        });
    }
    assert(count==w*h*4);
}

int main() {
    for(unsigned tile:{1u,8u,64u,512u,3000u,6000u})
        for(unsigned n:{1u,63u,64u,65u,513u,4608u,9216u,11637u,16384u}) Axis(n,tile);
    for(unsigned tile:{8u,64u,128u}) {
        Identity(1,1,tile); Identity(67,53,tile); Identity(270,299,tile);
        Identity(64,64,tile); Identity(513,8,tile);
    }
    puts("tile blend geometry, normalized coverage, context crop, HDR range and rolling rows: OK");
}
