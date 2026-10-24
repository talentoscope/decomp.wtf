# Commodore BASIC V2 Token Table (BASIC 2.0)

Single-byte keyword tokens used by the C64's Commodore BASIC V2 ROM.
Tokens are always `>= $80` (most-significant bit set), which is how the
interpreter distinguishes a token from plain PETSCII text. Source:
https://www.c64-wiki.com/wiki/BASIC_token

## Full table

| Byte | Hex  | Keyword        | Byte | Hex  | Keyword          | Byte | Hex  | Keyword        | Byte | Hex  | Keyword          |
|------|------|----------------|------|------|------------------|------|------|----------------|------|------|------------------|
| 128  | $80  | END            | 147  | $93  | LOAD             | 166  | $A6  | SPC(            | 185  | $B9  | POS             |
| 129  | $81  | FOR            | 148  | $94  | SAVE             | 167  | $A7  | THEN            | 186  | $BA  | SQR             |
| 130  | $82  | NEXT           | 149  | $95  | VERIFY           | 168  | $A8  | NOT             | 187  | $BB  | RND             |
| 131  | $83  | DATA           | 150  | $96  | DEF              | 169  | $A9  | STEP            | 188  | $BC  | LOG             |
| 132  | $84  | INPUT#         | 151  | $97  | POKE             | 170  | $AA  | + (Addition)   | 189  | $BD  | EXP             |
| 133  | $85  | INPUT          | 152  | $98  | PRINT#           | 171  | $AB  | − (Subtraction)| 190  | $BE  | COS             |
| 134  | $86  | DIM            | 153  | $99  | PRINT            | 172  | $AC  | * (Multiply)   | 191  | $BF  | SIN             |
| 135  | $87  | READ           | 154  | $9A  | CONT             | 173  | $AD  | / (Division)   | 192  | $C0  | TAN             |
| 136  | $88  | LET            | 155  | $9B  | LIST             | 174  | $AE  | ^ (Power)      | 193  | $C1  | ATN             |
| 137  | $89  | GOTO           | 156  | $9C  | CLR              | 175  | $AF  | AND             | 194  | $C2  | PEEK            |
| 138  | $8A  | RUN            | 157  | $9D  | CMD              | 176  | $B0  | OR              | 195  | $C3  | LEN             |
| 139  | $8B  | IF             | 158  | $9E  | SYS              | 177  | $B1  | > (greater-than)| 196  | $C4  | STR$            |
| 140  | $8C  | RESTORE        | 159  | $9F  | OPEN             | 178  | $B2  | = (equals)     | 197  | $C5  | VAL             |
| 141  | $8D  | GOSUB          | 160  | $A0  | CLOSE            | 179  | $B3  | < (less-than)  | 198  | $C6  | ASC             |
| 142  | $8E  | RETURN         | 161  | $A1  | GET              | 180  | $B4  | SGN             | 199  | $C7  | CHR$            |
| 143  | $8F  | REM            | 162  | $A2  | NEW              | 181  | $B5  | INT             | 200  | $C8  | LEFT$           |
| 144  | $90  | STOP           | 163  | $A3  | TAB(             | 182  | $B6  | ABS             | 201  | $C9  | RIGHT$          |
| 145  | $91  | ON             | 164  | $A4  | TO               | 183  | $B7  | USR             | 202  | $CA  | MID$            |
| 146  | $92  | WAIT           | 165  | $A5  | FN               | 184  | $B8  | FRE             | 203  | $CB  | GO              |

## Grouping

- **Commands:**        128–162 / `$80`–`$A2`
- **Bywords:**         163–169 / `$A3`–`$A9`
- **Operators:**       170–179 / `$AA`–`$B3`
- **Functions:**       180–202 / `$B4`–`$CA`
- **Outlier command:** `GO` is at 203 / `$CB`

Code 255 / `$FF` is reserved for the "pi" character; 204–254 (`$CC`–`$FE`)
are unused by stock BASIC 2.0. BASIC expansions may occupy these.

## Python lookup (for later parsing use)

```python
# BASIC 2.0 token -> keyword
BASIC_TOKENS = {
    0x80: "END", 0x81: "FOR", 0x82: "NEXT", 0x83: "DATA", 0x84: "INPUT#",
    0x85: "INPUT", 0x86: "DIM", 0x87: "READ", 0x88: "LET", 0x89: "GOTO",
    0x8A: "RUN", 0x8B: "IF", 0x8C: "RESTORE", 0x8D: "GOSUB", 0x8E: "RETURN",
    0x8F: "REM", 0x90: "STOP", 0x91: "ON", 0x92: "WAIT", 0x93: "LOAD",
    0x94: "SAVE", 0x95: "VERIFY", 0x96: "DEF", 0x97: "POKE", 0x98: "PRINT#",
    0x99: "PRINT", 0x9A: "CONT", 0x9B: "LIST", 0x9C: "CLR", 0x9D: "CMD",
    0x9E: "SYS", 0x9F: "OPEN", 0xA0: "CLOSE", 0xA1: "GET", 0xA2: "NEW",
    0xA3: "TAB(", 0xA4: "TO", 0xA5: "FN", 0xA6: "SPC(", 0xA7: "THEN",
    0xA8: "NOT", 0xA9: "STEP", 0xAA: "+", 0xAB: "-", 0xAC: "*", 0xAD: "/",
    0xAE: "^", 0xAF: "AND", 0xB0: "OR", 0xB1: ">", 0xB2: "=", 0xB3: "<",
    0xB4: "SGN", 0xB5: "INT", 0xB6: "ABS", 0xB7: "USR", 0xB8: "FRE",
    0xB9: "POS", 0xBA: "SQR", 0xBB: "RND", 0xBC: "LOG", 0xBD: "EXP",
    0xBE: "COS", 0xBF: "SIN", 0xC0: "TAN", 0xC1: "ATN", 0xC2: "PEEK",
    0xC3: "LEN", 0xC4: "STR$", 0xC5: "VAL", 0xC6: "ASC", 0xC7: "CHR$",
    0xC8: "LEFT$", 0xC9: "RIGHT$", 0xCA: "MID$", 0xCB: "GO",
}
```
