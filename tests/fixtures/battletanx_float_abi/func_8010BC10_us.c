typedef float f32;
typedef double f64;
typedef int s32;

/* Four incoming eight-byte float pairs are passed in O32 word slots.
 * y precedes x, as the caller loads/stores and this callee's lwc1 establish. */
struct QueryPointPair { f32 y; f32 x; };

s32 func_8010BC10_us(struct QueryPointPair a, struct QueryPointPair b,
                    struct QueryPointPair c, struct QueryPointPair d,
                    struct QueryPointPair *out)
{
    f64 ay = (f64)(b.y - a.y);
    f64 ax = (f64)(b.x - a.x);
    f64 bx = (f64)(d.x - c.x);
    f64 by = (f64)(d.y - c.y);
    f64 rx = (f64)(a.x - c.x);
    f64 ry = (f64)(a.y - c.y);
    f64 denominator = bx * ay - by * ax;
    f64 t;
    f64 u;
    s32 result;

    if (denominator == 0.0) {
        if (rx * ay - ry * ax == 0.0) return 2;
        return 0;
    }
    t = (rx * ay - ry * ax) / denominator;
    u = (rx * by - ry * bx) / denominator;
    result = (((0.0 <= t) & (t <= 1.0)) &&
              ((0.0 <= u) & (u <= 1.0))) ? 1 : 0;
    /* Both outputs are written in the nonparallel case even on result zero. */
    out->x = (f32)((f64)c.x + t * (f64)(d.x - c.x));
    out->y = (f32)((f64)c.y + t * (f64)(d.y - c.y));
    return result;
}
