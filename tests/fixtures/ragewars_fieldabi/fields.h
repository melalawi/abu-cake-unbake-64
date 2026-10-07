typedef float f32;
typedef int s32;
struct Object_func_8023B3F8_de {
    char gap0[4];
    struct Object_func_8023B3F8_de *next;
    char gap8[0x208];
    f32 depth;
};
struct View_func_8023B3F8_de {
    char gap0[0x120];
    s32 hidden;
    char gap124[8];
    f32 depth;
};
struct Scene {
    char gap0[0x8B4];
    struct Object_func_8023B3F8_de *objects;
    char gap8B8[12];
    s32 count;
};
