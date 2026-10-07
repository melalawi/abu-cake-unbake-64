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
