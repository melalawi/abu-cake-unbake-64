#ifndef UNBAKE_SPAN_1000_CODE_802B369C_H
#define UNBAKE_SPAN_1000_CODE_802B369C_H
#include "acmd.h"
#include "audio_callbacks.h"
#include "../types.h"
#include "common/types_1dc8418c21db.h"
struct ALMainBus_s;
/* unbake published declaration: published_80538a407ee54023b0bcfd0c */
struct ALMainBus_s {
    ALFilter_s14 filter;
    s32 sourceCount;
    s32 maxSources;
    ALFilter_s14 **sources;
};
struct ALMainBus_s;
/* unbake published declaration: published_b4e0d50107330fc19ae907db */
typedef struct ALMainBus_s ALMainBus_s;
#endif
