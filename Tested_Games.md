# Tested games

rpgsync is tested on EasyRPG's engine test suites and on complete fan games.

## Compatibility

- **Engines:** RPG Maker 2000 and 2003, the official English 2003 release
  (Steam, `ultimate_rt_eb.dll`) included. RPG Maker XP, VX, MV and MZ use other
  formats and are not supported.
- **Game files:** the database (`RPG_RT.ldb`), the map tree (`RPG_RT.lmt`) and
  the maps (`Map*.lmu`). Saves (`.lsd`) and resources are not touched.
- **Players:** games keep running on RPG_RT and on
  [EasyRPG Player](https://easyrpg.org/player/).
- **Patches:** DynRPG, Maniac Patch, EasyRPG commands, Power Mode 2003, the
  Ineluki Key Patch and engine-only patches: see [Patches.md](Patches.md).
- **Protected games** whose file headers are scrambled (FRQ below).
- **Python 3.11+.** The runs below were made on macOS. rpgsync is plain Python
  and its output has a fallback for the Windows console, but Linux and Windows
  are not covered by these runs.

## What we run

Each game is copied and its copy synced from scratch:

```bash
rpgsync pull game/           # every map, the database and common events -> Python
rpgsync push game/           # Python -> game files, straight back
shasum game/*.ldb game/*.lmt game/*.lmu   # compared before / after the push
rpgsync status game/         # "All files are synced"?
rpgsync check game/          # compiles, fits the engine and patches, type checks
cd game/Scripts && uv run pytest   # the game's own generated tests
```

The push must leave every game file **byte for byte identical**: whatever
rpgsync can't express as readable Python is kept as raw `cmd(...)` lines,
never dropped.

## Results

| Game | Engine | Patches | Maps | Round trip | `check` | Generated tests | Raw `cmd()` lines |
|---|---|---|---|---|---|---|---|
| [TestGame-2000](https://github.com/EasyRPG/TestGame) | 2000 | none | 80 | identical | 154 problems (deliberately invalid values) | 174 passed, 7 failed | 126 / 169k |
| [TestGame-2003](https://github.com/EasyRPG/TestGame) | 2003 | DynRPG | 20 | identical | ✅ | 67 passed | 34 / 18k |
| [TestGame-EasyRPG](https://github.com/EasyRPG/TestGame) | 2003 | Maniac, EasyRPG | 10 | identical | 4 problems (DynRPG used without the patch) | 46 passed, 1 failed | 866 / 21k |
| [TestGame-Maniac](https://github.com/EasyRPG/TestGame) | 2003 | Maniac | 46 | identical | 2 problems (Maniac values out of range) | 118 passed, 1 failed | 1,723 / 35k |
| [TestGame-PowerMode2003](https://github.com/EasyRPG/TestGame) | 2003 | Power Mode 2003 | 1 | identical | ✅ | 29 passed | 213 / 12k |
| [AA City](https://e-magination.jeun.fr/t4629-aa-city?highlight=aa+city) (alpha 0.3.3) | 2003 | Power Mode 2003 | 4 | identical | ✅ | 35 passed | 0 / 88k |
| [Les Secrets de Hertepay](https://e-magination.jeun.fr/t5002-concours-automne-2019-les-secrets-de-hertepay-qui-a-supprime-frq) | 2003 | DynRPG | 24 | identical | ✅ | 75 passed | 0 / 27k |
| [FRQ](https://www.guelnika.net/jeux) (Demo 3) | 2003 | DynRPG, protected headers | 499 | identical | ✅ | 1,025 passed | 0 / 490k |
| Off | 2003 | none | 347 | identical | ✅ | 721 passed | 0 / 82k |
| [A Tale of Yu](https://www.alexdor.info/?p=jeu&id=1056) (Onsen RPG) | 2003, official English release | none | 64 | identical | ✅ | 155 passed | 0 / 88k |
| [Final Chronicle](https://www.alexdor.info/?p=jeu&id=1055) | 2003, official English release | none | 105 | identical | ✅ | 237 passed | 0 / 77k |

Raw lines are counted against all the script lines of the game.

### Notes

- **The TestGame suites** ([EasyRPG/TestGame](https://github.com/EasyRPG/TestGame))
  test the engine, edge cases included: TestGame-2000 uses out-of-range values
  and RPG Maker 2003 parameters on purpose, which `check` reports.
- **Raw lines** in the TestGames are mostly commands stored with an unusual
  parameter layout (a missing trailing parameter, an unused field set) and
  EasyRPG's own commands: rpgsync only writes Python that compiles back to the
  very same bytes. Maniac Patch commands are written `maniac.name(...)` (about
  1,500 of them in the two Maniac TestGames), with named arguments where their
  layout is known.
- **The official English RPG Maker 2003** (Steam, `ultimate_rt_eb.dll`):
  A Tale of Yu and Final Chronicle.
- **Patches** are detected from the game: `DynPlugins/`, and the `[Patch]`
  section of `EasyRPG.ini`.
- **Protected games** (FRQ) scramble the files' headers so that the editor
  refuses them: rpgsync reads them and writes the header back unchanged.
- **Message lines holding a line break** (text pasted into the editor, as in
  Final Chronicle) are written `text(..., split=False)`.
