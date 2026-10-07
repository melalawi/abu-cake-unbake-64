#ifndef UNBAKE_COMMON_UNUSED_H
#define UNBAKE_COMMON_UNUSED_H
#include "audio_callbacks.h"
#include "../types.h"
#include "common/types_1dc8418c21db.h"
struct ALMainBus_s;
typedef struct ALMainBus_s ALMainBus_s;
struct ALMainBus_s;
struct ALMainBus_s {
    ALFilter_s14 filter;
    s32 sourceCount;
    s32 maxSources;
    ALFilter_s14 **sources;
};
#endif
