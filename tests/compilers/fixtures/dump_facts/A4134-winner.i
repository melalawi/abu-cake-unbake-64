typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef struct AnimationFrameState {
    u8 node_prefix[0x2C];
    f32 endpoint;
    f32 frame;
    u8 state_between_frame_and_cache[4];
    s32 cached_frame;
} AnimationFrameState;
typedef struct AnimationFrameDefinition {
    u8 definition_prefix[0x1E];
    u8 mode;
} AnimationFrameDefinition;
extern s32 func_802744D4_de(void);
s32 func_802A4134_de(AnimationFrameState *state,
                     AnimationFrameDefinition *definition, s32 *frame_count) {
    s32 frame = (s32)state->frame;
    s32 *selected = &frame;
    s32 value;
    switch (definition->mode) {
    case 0:
        value = *frame_count - 1;
        if (*selected >= value) {
            *selected = value;
            state->endpoint = (f32)*frame_count;
        }
        break;
    case 1:
        if (*selected >= *frame_count) {
            *selected = *frame_count - 1;
        }
        break;
    case 2:
        *selected %= *frame_count;
        break;
    case 3:
        value = *frame_count * 2 - 2;
        if (value > 0) {
            value = *selected % value;
        } else {
            value = 0;
        }
        *selected = value;
        if (value >= *frame_count) {
            s32 reflected = *frame_count * 2;
            s32 adjusted = value + 2;
            *selected = reflected - adjusted;
        }
        break;
    case 4:
        *selected = func_802744D4_de() % *frame_count;
        break;
    case 5:
        if (state->cached_frame == -1) {
            state->cached_frame = func_802744D4_de() % *frame_count;
        }
        *selected = state->cached_frame;
        break;
    }
    return frame;
}
