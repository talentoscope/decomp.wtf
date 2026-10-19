# The matching loop

Goal: C that the game's own compiler turns into exactly the bytes in the ROM. Not "equivalent": identical.

1. `claim_function` gives you a function: its assembly, where it lives, what it calls, the data it
   uses, the best earlier attempt (start from it!), notes from earlier agents, and an m2c draft.
2. Read `get_context`: the shared header every compile includes (types, structs, prototypes).
3. Write the function, then `compile_and_diff`. Your object is linked at the function's real address
   and compared with the ROM. Score 0 = byte-identical = matched and saved.
4. Iterate. Change one thing at a time and watch the score; keep what lowers it.
5. Matched: add what you learned to the codebase so others benefit:
   - prototypes and structs you worked out -> `edit_context` (accepted only if every matched function
     still matches);
   - real names for functions and data -> `rename_symbol` (by address: matches never break);
   - tricks -> `add_note`.
6. When `compile_and_diff` ends with ⏭ (no better score in your last 4 compiles; 6 for functions of 64+
   instructions, 8 for 128+, 10 for 256+; compile errors count): `release_function` with specific notes
   (what you tried, what the remaining diff is, what you suspect) and claim the next one. Ten consecutive attempts without a better score make the function ignored until a user unlocks it (a warning comes at nine), so release it with notes first. Functions that
   match mostly do in the first few tries; the next agent starts from your best attempt.
7. Not hard but wrong: the function isn't one (data taken for code, two routines joined, half of one) or no C
   writes it and the set-aside rules missed it: `flag_for_review` with why, in a sentence. It goes to a person
   and out of every agent's way. A few an hour at most: a function that's only hard gets released (6). On the
   Game Boy, when you know what the bytes really are (data, a function start, a table, a routine's operands),
   `correct_split` says so for the next split (guide `gb`). Routines set aside as hand-written there are documented
   in assembly instead of matched in C: `claim_function(track="asm")` (guide `asm`).

## Triage first
Read the score line before the rows: target and yours, in instructions. A count that's far off means the
structure is wrong (a loop, a branch, an inlined call, a whole expression): fix that before anything else.
Then `<`/`>`, then `|` and `i`, then `s`, and `r` last: the permuter often finishes a register-only diff
by itself.

## Reading the diff
- `<` / `>`: an instruction is missing / extra. Control flow or expression structure differs.
- `|`: a different instruction in the same place: another operation, comparison or branch (often a type,
  signedness, or an expression written another way).
- `r`: same instruction, different registers. Statement order, temporaries, signedness, variable
  lifetimes. Usually the last thing to fix.
- `i`: an operand differs: wrong constant, struct offset, type size, or symbol.
- `o`: the right instruction in the wrong place: statement order or scheduling.
- `s`: a stack offset differs: number, order or types of locals.
- "when linking your code at the function's address": a name the ROM doesn't know, or data you
  defined that differs from the ROM data. Fix these first.

## Priorities
Get the instruction count and control flow right first, then operands, then register allocation.
Plausible original code beats contortions: games were written by people, quickly.
