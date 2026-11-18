__device__ int reflect(int i, int n, int mode) {
    if (mode == 0) return max(0, min(i, n - 1));
    if (n == 1) return 0;
    int period = 2 * n - (mode == 2 ? 2 : 0);
    i %= period;
    if (i < 0) i += period;
    return i < n ? i : period - i - (mode == 2 ? 0 : 1);
}
__device__ float unit(float x) { return fminf(1, fmaxf(0, x)); }
__device__ float linear(float x, int adobe) {
    return adobe ? powf(unit(x), 2.19921875f)
                 : (x <= .04045f ? x / 12.92f : powf((x + .055f) / 1.055f, 2.4f));
}
__device__ float encoded(float x, int adobe) {
    x = unit(x);
    return adobe ? powf(x, 1.f / 2.19921875f)
                 : (x <= .0031308f ? x * 12.92f : 1.055f * powf(x, 1.f / 2.4f) - .055f);
}
#define PIXEL(n) int i = blockIdx.x * blockDim.x + threadIdx.x; if (i >= (n)) return

extern "C" __global__ void luminance(const float *rgb, float *y, float *log_y, int n, int adobe) {
    PIXEL(n);
    float lum = linear(rgb[3*i], adobe) * (adobe ? .0753f : .0722f)
              + linear(rgb[3*i+1], adobe) * (adobe ? .6273f : .7152f)
              + linear(rgb[3*i+2], adobe) * (adobe ? .2974f : .2126f);
    y[i] = lum;
    log_y[i] = logf(lum + .0001f);
}
extern "C" __global__ void area(const float *src, float *out, int w, int h, int ow, int oh) {
    PIXEL(ow * oh);
    double sx = (double)w / ow, sy = (double)h / oh;
    double x0 = (i % ow) * sx, x1 = x0 + sx, y0 = (i / ow) * sy, y1 = y0 + sy;
    float sum = 0;
    for (int y = (int)y0; y < min(h, (int)ceil(y1)); ++y)
        for (int x = (int)x0; x < min(w, (int)ceil(x1)); ++x) {
            double weight = (fmin(x1, x + 1.) - fmax(x0, (double)x))
                          * (fmin(y1, y + 1.) - fmax(y0, (double)y)) / (sx * sy);
            sum += src[y*w+x] * (float)weight;
        }
    out[i] = sum;
}
__device__ void cubic_weights(float x, float *k) {
    float a = -.75f, t = x + 1.f, s = 1.f - x;
    k[0] = ((a*t-5*a)*t+8*a)*t-4*a;
    k[1] = ((a+2)*x-(a+3))*x*x+1;
    k[2] = ((a+2)*s-(a+3))*s*s+1;
    k[3] = 1.f-k[0]-k[1]-k[2];
}
extern "C" __global__ void cubic(const float *src, float *out, int w, int h, int ow, int oh) {
    PIXEL(ow * oh);
    float x = (float)(((i % ow) + .5) * ((double)w / ow) - .5);
    float y = (float)(((i / ow) + .5) * ((double)h / oh) - .5);
    int ix = (int)floorf(x), iy = (int)floorf(y);
    float kx[4], ky[4], sum = 0;
    cubic_weights(x - ix, kx); cubic_weights(y - iy, ky);
    for (int j = 0; j < 4; ++j) {
        float row = 0;
        for (int k = 0; k < 4; ++k)
            row += kx[k] * src[reflect(iy+j-1,h,0)*w + reflect(ix+k-1,w,0)];
        sum += row * ky[j];
    }
    out[i] = sum;
}
extern "C" __global__ void gaussian(const float *src, float *out, const float *weights,
                                    int w, int h, int r, int axis, int border, int channels, int start, int end) {
    PIXEL(w*h*channels);
    int px = i/channels, x = px%w, y = px/w;
    float sum = start == 0 ? 0.f : out[i];
    int pos = axis ? y : x, length = axis ? h : w, stride = (axis ? w : 1)*channels;
    if (pos + (start-r) >= 0 && pos + (end-1-r) < length) {
        for (int k = start; k < end; ++k) sum += src[i+(k-r)*stride] * weights[k];
    } else {
        for (int k = start; k < end; ++k) {
            int j = i + (reflect(pos+(k-r),length,border)-pos)*stride;
            sum += src[j] * weights[k];
        }
    }
    out[i] = sum;
}
__device__ float mask_ramp(float x, float start, float width) {
    float t = unit((x-start)/width);
    return t*t*(3.f-2.f*t);
}
extern "C" __global__ void mask_feather(float *base, const float *blurred, int n, int inward) {
    PIXEL(n);
    base[i] = inward ? fminf(base[i], unit(2.f*blurred[i]-1.f)) : blurred[i];
}
extern "C" __global__ void mask_color_range(const float *bgr, float *out, int n,
                                            float hue, float sat, float h0, float hw, float s0, float sw) {
    PIXEL(n);
    const float b=bgr[3*i], g=bgr[3*i+1], r=bgr[3*i+2];
    const float v=fmaxf(r,fmaxf(g,b)), d=v-fminf(r,fminf(g,b)), eps=0x1p-23f;
    float hue_weight=1.f;
    if (sat >= .05f) {
        float factor=60.f/(d+eps);
        float h = v == r ? (g-b)*factor : v == g ? (b-r)*factor+120.f : (r-g)*factor+240.f;
        if (h < 0.f) h += 360.f;
        float delta=fabsf(h-hue);
        hue_weight=1.f-mask_ramp(fminf(delta,360.f-delta),h0,hw);
    }
    out[i]=unit(hue_weight*(1.f-mask_ramp(fabsf(d/(fabsf(v)+eps)-sat),s0,sw)));
}
extern "C" __global__ void mask_tone(const float *logs, float *mask, int n, int adobe, int has_base,
                                    float sh, float sw, float hi, float hw, float amount) {
    PIXEL(n);
    float v = encoded(expf(logs[i]), adobe);
    float tone = mask_ramp(v,sh,sw) * (1.f-mask_ramp(v,hi,hw));
    float keep = 1.f - amount*(1.f-tone);
    mask[i] = has_base ? mask[i]*keep : keep;
}
extern "C" __global__ void mask_hessian(const float *g, float *resp, int w, int h, float sigma) {
    PIXEL(w*h);
    int x=i%w,y=i/w;
    int xm=reflect(x-1,w,2), xp=reflect(x+1,w,2);
    int ym=reflect(y-1,h,2)*w, yc=y*w, yp=reflect(y+1,h,2)*w;
    float tl=g[ym+xm], tc=g[ym+x], tr=g[ym+xp];
    float ml=g[yc+xm], mc=g[i], mr=g[yc+xp];
    float bl=g[yp+xm], bc=g[yp+x], br=g[yp+xp];
    float a=(((tl+tr)-2.f*tc)+2.f*((ml+mr)-2.f*mc)+((bl+br)-2.f*bc))*0.125f;
    float b=((br-bl)-(tr-tl))*0.25f;
    float c=(((bl+tl)-2.f*ml)+2.f*((bc+tc)-2.f*mc)+((br+tr)-2.f*mr))*0.125f;
    float d=(a-c)*0.5f, r=sqrtf(d*d+b*b), mid=(a+c)*0.5f;
    float delta=fabsf(fabsf(mid+r)-fabsf(mid-r));
    resp[i]=fmaxf(resp[i],delta*sigma*sigma);
}
extern "C" __global__ void mask_structure_ramp(float *resp, int n, float threshold, float width) {
    PIXEL(n);
    resp[i]=mask_ramp(resp[i],threshold,width);
}
extern "C" __global__ void mask_apply(const float *weights, float *mask, int n, int has_base, float amount, int inverse) {
    PIXEL(n);
    float keep=1.f-amount*(inverse ? 1.f-weights[i] : weights[i]);
    mask[i]=has_base ? mask[i]*keep : keep;
}
extern "C" __global__ void box_rows(const float *src, double *out, int w, int h, int r) {
    PIXEL(h);
    int start = i*w;
    double sum = 0;
    for (int k = -r; k <= r; ++k) sum += src[start + reflect(k,w,1)];
    for (int k = 0; k < w; ++k) {
        out[start+k] = sum;
        sum += (double)src[start+reflect(k+r+1,w,1)] - src[start+reflect(k-r,w,1)];
    }
}
extern "C" __global__ void box_cols(const double *src, float *out, int w, int h, int r) {
    PIXEL(w);
    double sum = 0, inv = 1. / ((2.*r+1)*(2.*r+1));
    for (int k = -r; k <= r; ++k) sum += src[reflect(k,h,1)*w+i];
    for (int k = 0; k < h; ++k) {
        out[k*w+i] = (float)(sum*inv);
        sum += src[reflect(k+r+1,h,1)*w+i] - src[reflect(k-r,h,1)*w+i];
    }
}
extern "C" __global__ void product(const float *a, const float *b, float *out, int n) {
    PIXEL(n); out[i] = a[i] * b[i];
}
extern "C" __global__ void sharpen_skin(float *mask, int n) {
    PIXEL(n);
    float t = unit((unit(mask[i]) - 0.35f) / 0.30f);
    mask[i] = t * t * (3.f - 2.f * t);
}
extern "C" __global__ void invert(float *layer, int n) {
    PIXEL(n); layer[i] = 1.f - layer[i];
}
extern "C" __global__ void coefficients(float *a, float *b, const float *mg, const float *ms, int n, float eps) {
    PIXEL(n);
    a[i] = (a[i] - mg[i]*ms[i]) / ((b[i] - mg[i]*mg[i]) + eps);
    b[i] = ms[i] - a[i]*mg[i];
}
extern "C" __global__ void guided(const float *guide, const float *a, const float *b, float *out, int n) {
    PIXEL(n); out[i] = a[i]*guide[i]+b[i];
}
extern "C" __global__ void mask_stats(const float *l, const float *m, double *totals, int n) {
    __shared__ double sums[4][128];
    int t = threadIdx.x, i = blockIdx.x * blockDim.x + t;
    float x = i < n ? l[i] : 0, v = i < n ? m[i] : 0;
    sums[0][t] = x; sums[1][t] = v > .5f ? x : 0;
    sums[2][t] = v > .5f; sums[3][t] = v >= .001f;
    __syncthreads();
    for (int step = 64; step > 0; step /= 2) {
        if (t < step) for (int k = 0; k < 4; ++k) sums[k][t] += sums[k][t+step];
        __syncthreads();
    }
    if (t == 0) for (int k = 0; k < 4; ++k) atomicAdd(totals+k, sums[k][0]);
}
extern "C" __global__ void fill_mask(float *l, const float *m, const float *n1, const float *d1,
                                     const float *n2, const float *d2, const double *totals, int size) {
    PIXEL(size);
    float mean = totals[2] ? (float)(totals[1]/totals[2]) : (float)(totals[0]/size);
    float v1 = n1[i]/fmaxf(d1[i], .001f);
    float v2 = d2[i] > .001f ? n2[i]/d2[i] : mean;
    float t = unit((d1[i]-.02f)/.28f), k = t*t*(3-2*t);
    l[i] = m[i]*l[i] + (1-m[i])*(k*v1+(1-k)*v2);
}
extern "C" __global__ void accumulate(float *band, const float *prev, const float *next, int n, float gain) {
    PIXEL(n); band[i] += gain * (prev[i]-next[i]);
}
extern "C" __global__ void change(float *band, int n, float strength, float tau, float cap) {
    PIXEL(n);
    float b = band[i], t = b/tau;
    band[i] = fminf(cap, fmaxf(-cap, -strength*b*expf(-(t*t))));
}
extern "C" __global__ void layer(const float *y, const float *change, const float *plain,
                                 const float *weight, const double *stats, float *out, int n,
                                 float cap, int adobe) {
    PIXEL(n);
    float c = fminf(cap, fmaxf(-cap, change[i]));
    if (weight) c = (stats[3] ? c : 0) * weight[i]
                    + (1-weight[i]) * fminf(cap,fmaxf(-cap,plain[i]));
    float cb = encoded(y[i], adobe), target = encoded(y[i]*expf(c), adobe);
    float d = cb <= .25f ? ((16*cb-12)*cb+4)*cb : sqrtf(cb);
    out[i] = unit(target <= cb ? .5f-(cb-target)/(2*fmaxf(cb*(1-cb),1e-6f))
                              : .5f+(target-cb)/(2*fmaxf(d-cb,1e-6f)));
}
