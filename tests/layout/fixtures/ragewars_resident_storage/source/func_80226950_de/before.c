#include "common/unused.h"
#include "span_1000/code_80225D10.h"

/** Allocate and clear the player array and its per-player state blocks, linking each player to its block. */
extern char D_800C2B68_de[];
extern void **func_8025343C_de(int, int, int, char *);
extern void func_802A001C_de(void *, int, int);

void func_80226950_de(PlayerList *list, int count) {
    int playerSize = count * sizeof(Player_func_80226950_de);
    int stateSize = count * 0x330;
    char *states;
    void **handle;
    int i;

    handle = func_8025343C_de(0, playerSize + stateSize, 0x23, D_800C2B68_de);
    list->handle = handle;
    list->count = count;
    list->players = *handle;
    states = (char *)list->players + playerSize;
    func_802A001C_de(list->players, 0, playerSize);
    func_802A001C_de(states, 0, stateSize);
    for (i = 0; i < count; i++) {
        list->players[i].state = states + i * 0x330;
    }
}

/* Native resident constant storage; absolute access symbols retain their addresses. */
#if defined(VERSION_US)
const double unbake_rodata_800C5170_8 = 4294967296.0;
const float unbake_rodata_800C5178_4 = 0.00392156886f;
const float unbake_rodata_800C517C_4 = 0.5f;
const float unbake_rodata_800C5180_4 = 1.0f;
const float unbake_rodata_800C5184_4 = 1.0f;
const float unbake_rodata_800C5188_4 = 0.5f;
const float unbake_rodata_800C518C_4 = 255.0f;
const float unbake_rodata_800C5190_4 = 5.0f;
const float unbake_rodata_800C5194_4 = 1.0f;
const float unbake_rodata_800C5198_4 = 2.14748365e+09f;
const float unbake_rodata_800C519C_4 = 2.14748365e+09f;
const float unbake_rodata_800C51A0_4 = 2.14748365e+09f;
const float unbake_rodata_800C51A4_4 = 2.14748365e+09f;
const float unbake_rodata_800C51A8_4 = 2.14748365e+09f;
const float unbake_rodata_800C51AC_4 = 2.14748365e+09f;
const float unbake_rodata_800C51B0_4 = 127.0f;
#elif defined(VERSION_US_REV1)
const double unbake_rodata_800CA330_8 = 4294967296.0;
const float unbake_rodata_800CA338_4 = 0.00392156886f;
const float unbake_rodata_800CA33C_4 = 0.5f;
const float unbake_rodata_800CA340_4 = 1.0f;
const float unbake_rodata_800CA344_4 = 1.0f;
const float unbake_rodata_800CA348_4 = 0.5f;
const float unbake_rodata_800CA34C_4 = 255.0f;
const float unbake_rodata_800CA350_4 = 5.0f;
const float unbake_rodata_800CA354_4 = 1.0f;
const float unbake_rodata_800CA358_4 = 2.14748365e+09f;
const float unbake_rodata_800CA35C_4 = 2.14748365e+09f;
const float unbake_rodata_800CA360_4 = 2.14748365e+09f;
const float unbake_rodata_800CA364_4 = 2.14748365e+09f;
const float unbake_rodata_800CA368_4 = 2.14748365e+09f;
const float unbake_rodata_800CA36C_4 = 2.14748365e+09f;
const float unbake_rodata_800CA370_4 = 127.0f;
#elif defined(VERSION_EU)
const float unbake_rodata_800C50F0_4 = 65536.0f;
const float unbake_rodata_800C50F4_4 = 65536.0f;
const float unbake_rodata_800C50F8_4 = 262144.0f;
#elif defined(VERSION_EU_X)
const float unbake_rodata_800C5000_4 = 10.2399998f;
const float unbake_rodata_800C5004_4 = 102400.0f;
const float unbake_rodata_800C5008_4 = 0.785398245f;
const float unbake_rodata_800C500C_4 = 0.305175781f;
const float unbake_rodata_800C5010_4 = 200.0f;
const float unbake_rodata_800C5014_4 = 255.0f;
const float unbake_rodata_800C5018_4 = 7.67999983f;
const float unbake_rodata_800C501C_4 = 8.0f;
const float unbake_rodata_800C5020_4 = 0.100000001f;
const float unbake_rodata_800C5024_4 = 2.14748365e+09f;
#elif defined(VERSION_DE)
const float unbake_rodata_800C5208_4 = 0.5f;
#endif
