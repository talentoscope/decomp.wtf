/* Trusted PS1 GTE intrinsics. Opcode/name table derived from Rabbitizer
 * revision 724a49a5b4dbfb99f1a9e6992e63964fd29c90c8, docs/r3000gte/gte_macros.h.
 * Memory operands and bounded register helpers are maintained by decomp.wtf.
 *
 * MIT License
 *
 * Copyright (c) 2022 Decompollaborate
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
#ifndef DECOMP_PSX_GTE_H
#define DECOMP_PSX_GTE_H

#define gte_RTPS() __asm__ __volatile__(".word 0x4A180001")
#define gte_nRTPS() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4A180001")
#define gte_rtps() gte_nRTPS()

#define gte_RTPT() __asm__ __volatile__(".word 0x4A280030")
#define gte_nRTPT() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4A280030")
#define gte_rtpt() gte_nRTPT()

#define gte_DPCL() __asm__ __volatile__(".word 0x4A680029")
#define gte_nDPCL() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4A680029")
#define gte_dpcl() gte_nDPCL()

#define gte_DPCS() __asm__ __volatile__(".word 0x4A780010")
#define gte_nDPCS() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4A780010")
#define gte_dpcs() gte_nDPCS()

#define gte_DPCT() __asm__ __volatile__(".word 0x4AF8002A")
#define gte_nDPCT() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4AF8002A")
#define gte_dpct() gte_nDPCT()

#define gte_INTPL() __asm__ __volatile__(".word 0x4A980011")
#define gte_nINTPL() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4A980011")
#define gte_intpl() gte_nINTPL()

#define gte_NCS() __asm__ __volatile__(".word 0x4AC8041E")
#define gte_nNCS() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4AC8041E")
#define gte_ncs() gte_nNCS()

#define gte_NCT() __asm__ __volatile__(".word 0x4AD80420")
#define gte_nNCT() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4AD80420")
#define gte_nct() gte_nNCT()

#define gte_NCDS() __asm__ __volatile__(".word 0x4AE80413")
#define gte_nNCDS() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4AE80413")
#define gte_ncds() gte_nNCDS()

#define gte_NCDT() __asm__ __volatile__(".word 0x4AF80416")
#define gte_nNCDT() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4AF80416")
#define gte_ncdt() gte_nNCDT()

#define gte_NCCS() __asm__ __volatile__(".word 0x4B08041B")
#define gte_nNCCS() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4B08041B")
#define gte_nccs() gte_nNCCS()

#define gte_NCCT() __asm__ __volatile__(".word 0x4B18043F")
#define gte_nNCCT() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4B18043F")
#define gte_ncct() gte_nNCCT()

#define gte_CDP() __asm__ __volatile__(".word 0x4B280414")
#define gte_nCDP() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4B280414")
#define gte_cdp() gte_nCDP()

#define gte_NCLIP() __asm__ __volatile__(".word 0x4B400006")
#define gte_nNCLIP() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4B400006")
#define gte_nclip() gte_nNCLIP()

#define gte_AVSZ3() __asm__ __volatile__(".word 0x4B58002D")
#define gte_nAVSZ3() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4B58002D")
#define gte_avsz3() gte_nAVSZ3()

#define gte_AVSZ4() __asm__ __volatile__(".word 0x4B68002E")
#define gte_nAVSZ4() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4B68002E")
#define gte_avsz4() gte_nAVSZ4()

#define gte_SQR(sf) __asm__ __volatile__(".word %0" : : "i"((0x4AA00428 | (((sf) & 1) << 19))))
#define gte_nSQR(sf) __asm__ __volatile__("nop\n\tnop\n\t.word %0" : : "i"((0x4AA00428 | (((sf) & 1) << 19))))
#define gte_sqr(sf) gte_nSQR(sf)

#define gte_OP(sf) __asm__ __volatile__(".word %0" : : "i"((0x4B70000C | (((sf) & 1) << 19))))
#define gte_nOP(sf) __asm__ __volatile__("nop\n\tnop\n\t.word %0" : : "i"((0x4B70000C | (((sf) & 1) << 19))))
#define gte_op(sf) gte_nOP(sf)

#define gte_GPF(sf) __asm__ __volatile__(".word %0" : : "i"((0x4B90003D | (((sf) & 1) << 19))))
#define gte_nGPF(sf) __asm__ __volatile__("nop\n\tnop\n\t.word %0" : : "i"((0x4B90003D | (((sf) & 1) << 19))))
#define gte_gpf(sf) gte_nGPF(sf)

#define gte_GPL(sf) __asm__ __volatile__(".word %0" : : "i"((0x4BA0003E | (((sf) & 1) << 19))))
#define gte_nGPL(sf) __asm__ __volatile__("nop\n\tnop\n\t.word %0" : : "i"((0x4BA0003E | (((sf) & 1) << 19))))
#define gte_gpl(sf) gte_nGPL(sf)

#define gte_MVMVA(sf,mx,v,cv,lm) __asm__ __volatile__(".word %0" : : "i"((0x4A400012 | (((sf)&1)<<19) | (((mx)&3)<<17) | (((v)&3)<<15) | (((cv)&3)<<13) | (((lm)&1)<<10))))
#define gte_nMVMVA(sf,mx,v,cv,lm) __asm__ __volatile__("nop\n\tnop\n\t.word %0" : : "i"((0x4A400012 | (((sf)&1)<<19) | (((mx)&3)<<17) | (((v)&3)<<15) | (((cv)&3)<<13) | (((lm)&1)<<10))))
#define gte_mvmva(sf,mx,v,cv,lm) gte_nMVMVA(sf,mx,v,cv,lm)


/* Register indices are always masked to the GTE register file. No helper accepts an instruction word. */
#define gte_mfc2(reg) __extension__ ({ int __decomp_gte_value; __asm__ __volatile__("mfc2 %0,$%1\n\tnop" : "=r"(__decomp_gte_value) : "i"((reg)&31)); __decomp_gte_value; })
#define gte_cfc2(reg) __extension__ ({ int __decomp_gte_value; __asm__ __volatile__("cfc2 %0,$%1\n\tnop" : "=r"(__decomp_gte_value) : "i"((reg)&31)); __decomp_gte_value; })
#define gte_mtc2(reg,value) __asm__ __volatile__("mtc2 %0,$%1" : : "r"(value), "i"((reg)&31))
#define gte_ctc2(reg,value) __asm__ __volatile__("ctc2 %0,$%1" : : "r"(value), "i"((reg)&31))
#define gte_lwc2(reg,p) __asm__ __volatile__("lwc2 $%1,0(%0)" : : "r"(p), "i"((reg)&31) : "memory")
#define gte_swc2(reg,p) __asm__ __volatile__("swc2 $%1,0(%0)" : : "r"(p), "i"((reg)&31) : "memory")
#define gte_ldv0(p) __asm__ __volatile__("lwc2 $0,0(%0)\n\tlwc2 $1,4(%0)" : : "r"(p) : "memory")
#define gte_ldv1(p) __asm__ __volatile__("lwc2 $2,0(%0)\n\tlwc2 $3,4(%0)" : : "r"(p) : "memory")
#define gte_ldv2(p) __asm__ __volatile__("lwc2 $4,0(%0)\n\tlwc2 $5,4(%0)" : : "r"(p) : "memory")
#define gte_ldv3(p0,p1,p2) __asm__ __volatile__("lwc2 $0,0(%0)\n\tlwc2 $1,4(%0)\n\tlwc2 $2,0(%1)\n\tlwc2 $3,4(%1)\n\tlwc2 $4,0(%2)\n\tlwc2 $5,4(%2)" : : "r"(p0), "r"(p1), "r"(p2) : "memory")
#define gte_ldrgb(p) gte_lwc2(6,p)
#define gte_lddp(value) gte_mtc2(8,value)
#define gte_stsxy(p) gte_swc2(14,p)
#define gte_stsxy0(p) gte_swc2(12,p)
#define gte_stsxy1(p) gte_swc2(13,p)
#define gte_stsxy2(p) gte_swc2(14,p)
#define gte_stsxy3(p0,p1,p2) __asm__ __volatile__("swc2 $12,0(%0)\n\tswc2 $13,0(%1)\n\tswc2 $14,0(%2)" : : "r"(p0), "r"(p1), "r"(p2) : "memory")
#define gte_stsz(p) gte_swc2(19,p)
#define gte_stsz3(p0,p1,p2) do { gte_swc2(17,p0); gte_swc2(18,p1); gte_swc2(19,p2); } while (0)
#define gte_stotz(p) gte_swc2(7,p)
#define gte_strgb(p) gte_swc2(22,p)
#define gte_stflg(p) do { int __decomp_gte_flag = gte_cfc2(31); *(int *)(p) = __decomp_gte_flag; } while (0)
#define gte_rtv0() gte_nMVMVA(1,0,0,3,0)
#define gte_rtv0tr() gte_nMVMVA(1,0,0,0,0)

#define gte_CC() __asm__ __volatile__(".word 0x4B38041C")
#define gte_nCC() __asm__ __volatile__("nop\n\tnop\n\t.word 0x4B38041C")
#define gte_cc() gte_nCC()
#define gte_dcpl() gte_nDPCL()
#define gte_gpf_lm(sf,lm) __asm__ __volatile__("nop\n\tnop\n\t.word %0" : : "i"(0x4B90003D | (((sf)&1)<<19) | (((lm)&1)<<10)))
#define gte_gpl_lm(sf,lm) __asm__ __volatile__("nop\n\tnop\n\t.word %0" : : "i"(0x4BA0003E | (((sf)&1)<<19) | (((lm)&1)<<10)))
#define gte_op_lm(sf,lm) __asm__ __volatile__("nop\n\tnop\n\t.word %0" : : "i"(0x4B70000C | (((sf)&1)<<19) | (((lm)&1)<<10)))

#endif
