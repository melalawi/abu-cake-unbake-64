#ifndef UNBAKE_COMMON_TYPES_1DC8418C21DB_H
#define UNBAKE_COMMON_TYPES_1DC8418C21DB_H
#include "audio_callbacks.h"
#include "../types.h"
struct ALFilter_s14;
/* unbake published declaration: published_81443b36fa92e493852c1f47 */
typedef struct ALFilter_s14 ALFilter_s14;

struct ALFilter_s14;
/* unbake published declaration: published_b56631290d354695c1fa2f29 */
struct ALFilter_s14 {
    struct ALFilter_s14 *source;
    void *handler;
    void *setParam;
    s16 inp;
    s16 outp;
    s32 type;
};

#endif
