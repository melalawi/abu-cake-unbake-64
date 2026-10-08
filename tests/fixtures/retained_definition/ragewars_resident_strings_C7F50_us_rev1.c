/* Resident player, weapon, pickup and menu labels. String slots include
 * their NUL terminator and original word alignment padding. */
struct MenuStrings_C7F50 {
    char label_select_team_0[12]; /* ROM0xC7F50 */
};
const struct MenuStrings_C7F50 ragewars_resident_strings_C7F50_us_rev1 = {
    "Select Team"
};
typedef char menu_strings_size_C7F50[(sizeof(struct MenuStrings_C7F50) == 12) ? 1 : -1];
