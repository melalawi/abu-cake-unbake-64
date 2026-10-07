typedef struct
{
  unsigned int w0;
  unsigned int w1;
} Awords;
typedef union
{
  Awords words;
  long long int force_union_align;
} Acmd;
typedef Acmd *(*ALCmdHandler)(void *, short *, int, int, Acmd *);
typedef int (*ALDMAproc)(int addr, int len, void *state);
typedef int (*ALVoiceHandler)(void *);
typedef int (*ALSetParam)(void *, int, void *);
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
struct Measured_func_800D1BA0_us_db74d82bc842
{
  unsigned char padding[8];
  int value;
};
struct Measured_func_800D1BA0_us_7cc74cb35a88
{
  unsigned char padding[12];
  int value;
};
struct Measured_func_800D1BA0_us_7d6aec9d411c
{
  unsigned char padding[28];
  int value;
};
struct Measured_func_800D1BA0_us_f88949686c7b
{
  unsigned char padding[20];
  int value;
};
struct Measured_func_800D1BA0_us_07091f3fea0d
{
  int value;
};
struct Measured_func_800D1BA0_us_5bc2a4c5e17f
{
  unsigned char padding[4];
  int value;
};
struct Measured_func_800D1BA0_us_95b7c07a1977
{
  unsigned char padding[16];
  int value;
};
struct Measured_func_800D1BA0_us_fc8673a50ef1
{
  unsigned char padding[24];
  int value;
};
struct Measured_func_800D1BA0_us_9e38496ccf6a
{
  unsigned char padding[32];
  int value;
};
struct Measured_func_800D1BA0_us_6846b134bee3
{
  unsigned char padding[40];
  int value;
};
struct Measured_func_800D1BA0_us_9610a1af3f4d
{
  unsigned char padding[56];
  int value;
};
struct Measured_func_800D1BA0_us_c1ec687bfaf5
{
  unsigned char padding[76];
  int value;
};
struct Measured_func_800D1BA0_us_0a3ee43c7f7f
{
  unsigned char padding[44];
  int value;
};
struct Measured_func_800D1BA0_us_ca6654f53bca
{
  unsigned char padding[48];
  int value;
};
struct Measured_func_800D1BA0_us_0305a1b2246e
{
  unsigned char padding[52];
  int value;
};
struct Measured_func_800D1BA0_us_3fb4ab99da79
{
  unsigned char padding[60];
  int value;
};
struct Measured_func_800D1BA0_us_dcc158b79400
{
  unsigned char padding[64];
  int value;
};
struct Measured_func_800D1BA0_us_71ab4e09f94a
{
  unsigned char padding[68];
  int value;
};
struct Measured_func_800D1BA0_us_cc3deb0ae0f2
{
  unsigned char padding[72];
  int value;
};
struct Measured_func_800D1BA0_us_028cd1a07375
{
  unsigned char padding[36];
  void *value;
};
struct Measured_func_800D1BA0_us_a3e8d4ed5eb7
{
  unsigned char padding[80];
  int value;
};
struct Measured_func_800D1BA0_us_83eb12cd79d5
{
  unsigned char padding[84];
  int value;
};
struct Measured_func_800D1BA0_us_473085b3fe0c
{
  unsigned char padding[88];
  int value;
};
struct Measured_func_800D1BA0_us_5cf0a8e1d176
{
  unsigned char padding[96];
  int value;
};
struct Measured_func_800D1BA0_us_ec3c26ac3a1a
{
  unsigned char padding[108];
  int value;
};
struct Measured_func_800D1BA0_us_564bd1a5830b
{
  unsigned char padding[120];
  int value;
};
struct Measured_func_800D1BA0_us_49e16976a508
{
  unsigned char padding[100];
  int value;
};
struct Measured_func_800D1BA0_us_ee1ad5d8443e
{
  unsigned char padding[104];
  int value;
};
struct Measured_func_800D1BA0_us_2ff3cb37a069
{
  unsigned char padding[112];
  int value;
};
struct Measured_func_800D1BA0_us_6555e0fc18dd
{
  unsigned char padding[116];
  int value;
};
struct Measured_func_800D1BA0_us_062923312951
{
  unsigned char padding[124];
  int value;
};
struct Measured_func_800D1BA0_us_81c1d4f60382
{
  unsigned char padding[128];
  int value;
};
struct Measured_func_800D1BA0_us_fda4d033572b
{
  unsigned char padding[132];
  int value;
};
struct Measured_func_800D1BA0_us_75bd156b4c2c
{
  unsigned char padding[92];
  void *value;
};
extern void *D_8037A174;
extern s32 D_8037ADCC;
void func_800D1BA0_us(s32 arg0)
{
  u32 temp_a0;
  s16 temp_a0_2;
  s32 temp_a1;
  u16 temp_v1;
  u16 var_t0;
  void *temp_a2;
  void *temp_v0;
  if (arg0 != 0)
  {
    temp_v0 = D_8037A174;
    ((struct Measured_func_800D1BA0_us_db74d82bc842 *) temp_v0)->value = 0xBA000E02;
    ((struct Measured_func_800D1BA0_us_7cc74cb35a88 *) temp_v0)->value = 0x8000;
    ((struct Measured_func_800D1BA0_us_7d6aec9d411c *) temp_v0)->value = -1;
    ((struct Measured_func_800D1BA0_us_f88949686c7b *) temp_v0)->value = 0x504240;
    ((struct Measured_func_800D1BA0_us_07091f3fea0d *) temp_v0)->value = 0xFC119623;
    ((struct Measured_func_800D1BA0_us_5bc2a4c5e17f *) temp_v0)->value = 0xFF2FFFFF;
    ((struct Measured_func_800D1BA0_us_95b7c07a1977 *) temp_v0)->value = 0xB900031D;
    ((struct Measured_func_800D1BA0_us_fc8673a50ef1 *) temp_v0)->value = 0xFA000101;
    ((struct Measured_func_800D1BA0_us_9e38496ccf6a *) temp_v0)->value = 0xFD500000;
    D_8037A174 = temp_v0 + 8;
    D_8037A174 = temp_v0 + 0x10;
    D_8037A174 = temp_v0 + 0x18;
    D_8037A174 = temp_v0 + 0x20;
    D_8037A174 = temp_v0 + 0x28;
    D_8037A174 = temp_v0 + 0x30;
    ((struct Measured_func_800D1BA0_us_6846b134bee3 *) temp_v0)->value = 0xF5500000;
    D_8037A174 = temp_v0 + 0x38;
    D_8037A174 = temp_v0 + 0x40;
    ((struct Measured_func_800D1BA0_us_9610a1af3f4d *) temp_v0)->value = 0xF3000000;
    D_8037A174 = temp_v0 + 0x48;
    D_8037A174 = temp_v0 + 0x50;
    ((struct Measured_func_800D1BA0_us_c1ec687bfaf5 *) temp_v0)->value = 0x200;
    ((struct Measured_func_800D1BA0_us_0a3ee43c7f7f *) temp_v0)->value = 0x07000200;
    ((struct Measured_func_800D1BA0_us_ca6654f53bca *) temp_v0)->value = 0xE6000000;
    ((struct Measured_func_800D1BA0_us_0305a1b2246e *) temp_v0)->value = 0;
    ((struct Measured_func_800D1BA0_us_3fb4ab99da79 *) temp_v0)->value = 0x071FF200;
    ((struct Measured_func_800D1BA0_us_dcc158b79400 *) temp_v0)->value = 0xE7000000;
    ((struct Measured_func_800D1BA0_us_71ab4e09f94a *) temp_v0)->value = 0;
    ((struct Measured_func_800D1BA0_us_cc3deb0ae0f2 *) temp_v0)->value = 0xF5400800;
    D_8037A174 = temp_v0 + 0x58;
    ((struct Measured_func_800D1BA0_us_028cd1a07375 *) temp_v0)->value = (void *) (D_8037ADCC + ((&D_8037ADCC) - 0x34));
    ((struct Measured_func_800D1BA0_us_a3e8d4ed5eb7 *) temp_v0)->value = 0xF2000000;
    ((struct Measured_func_800D1BA0_us_83eb12cd79d5 *) temp_v0)->value = 0xFC07C;
    ((struct Measured_func_800D1BA0_us_473085b3fe0c *) temp_v0)->value = 0xFD100000;
    var_t0 = 0;
    D_8037A174 = temp_v0 + 0x60;
    D_8037A174 = temp_v0 + 0x68;
    ((struct Measured_func_800D1BA0_us_5cf0a8e1d176 *) temp_v0)->value = 0xE8000000;
    D_8037A174 = temp_v0 + 0x70;
    ((struct Measured_func_800D1BA0_us_ec3c26ac3a1a *) temp_v0)->value = 0x07000000;
    D_8037A174 = temp_v0 + 0x78;
    D_8037A174 = temp_v0 + 0x80;
    ((struct Measured_func_800D1BA0_us_564bd1a5830b *) temp_v0)->value = 0xF0000000;
    ((struct Measured_func_800D1BA0_us_49e16976a508 *) temp_v0)->value = 0;
    ((struct Measured_func_800D1BA0_us_ee1ad5d8443e *) temp_v0)->value = 0xF5000100;
    ((struct Measured_func_800D1BA0_us_2ff3cb37a069 *) temp_v0)->value = 0xE6000000;
    ((struct Measured_func_800D1BA0_us_6555e0fc18dd *) temp_v0)->value = 0;
    ((struct Measured_func_800D1BA0_us_062923312951 *) temp_v0)->value = 0x0703C000;
    D_8037A174 = temp_v0 + 0x88;
    ((struct Measured_func_800D1BA0_us_81c1d4f60382 *) temp_v0)->value = 0xE7000000;
    ((struct Measured_func_800D1BA0_us_fda4d033572b *) temp_v0)->value = 0;
    ((struct Measured_func_800D1BA0_us_75bd156b4c2c *) temp_v0)->value = (void *) (D_8037ADCC + ((&D_8037ADCC) - 0x54));
    if (arg0 > 0)
    {
      do
      {
        temp_a2 = D_8037A174;
        D_8037A174 = temp_a2 + 8;
        D_8037A174 = temp_a2 + 0x10;
        D_8037A174 = temp_a2 + 0x18;
        ((struct Measured_func_800D1BA0_us_db74d82bc842 *) temp_a2)->value = 0xB4000000;
        ((struct Measured_func_800D1BA0_us_7cc74cb35a88 *) temp_a2)->value = 0;
        ((struct Measured_func_800D1BA0_us_95b7c07a1977 *) temp_a2)->value = 0xB3000000;
        ((struct Measured_func_800D1BA0_us_f88949686c7b *) temp_a2)->value = 0x04000400;
        temp_v1 = var_t0 / 3;
        temp_a0 = var_t0 % 3;
        var_t0 += 1;
        temp_a0_2 = (temp_a0 * 0x4B) + ((temp_v1 * 3) + 0x2E);
        temp_a1 = temp_v1 * 2;
        ((struct Measured_func_800D1BA0_us_07091f3fea0d *) temp_a2)->value = (s32) (((((temp_a0_2 + 0x32) * 4) & 0xFFF) << 0xC) | ((((temp_a1 + 0x89) * 4) & 0xFFF) | 0xE4000000));
        ((struct Measured_func_800D1BA0_us_5bc2a4c5e17f *) temp_a2)->value = (s16) ((((temp_a0_2 * 4) & 0xFFF) << 0xC) | (((temp_a1 + 0x78) * 4) & 0xFFF));
      }
      while (var_t0 < arg0);
    }
  }
}
