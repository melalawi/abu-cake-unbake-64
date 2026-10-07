#include "span_1000/code_802A25C4.h"
#include "span_16E000/code_8044E2B8.h"
#include "types.h"

extern char D_801428E0;

s32 func_8044DCC0_de(void) {
    Vtx *vertices = (Vtx *)&D_801428E0;
    s32 i;

    vertices[0].x = -1;
    vertices[0].z = 1;
    vertices[0].s = 0;
    vertices[0].t = 0;
    vertices[1].x = 1;
    vertices[1].z = 1;
    vertices[1].s = 1024;
    vertices[1].t = 0;
    vertices[2].x = 1;
    vertices[2].z = -1;
    vertices[2].s = 1024;
    vertices[2].t = 1024;
    vertices[3].x = -1;
    vertices[3].z = -1;
    vertices[3].s = 0;
    vertices[3].t = 1024;
    for (i = 0; i < 4; i++) {
        vertices[i].y = 0;
        vertices[i].flag = 0;
        vertices[i].r = 255;
        vertices[i].g = 255;
        vertices[i].b = 255;
        vertices[i].a = 255;
    }
    return 0;
}
