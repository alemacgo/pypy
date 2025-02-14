import sys

import py

from pypy import conftest
from pypy.jit.codewriter.policy import JitPolicy, StopAtXPolicy
from pypy.jit.metainterp import pyjitpl, history
from pypy.jit.metainterp.optimizeopt import ALL_OPTS_DICT
from pypy.jit.metainterp.test.support import LLJitMixin, OOJitMixin, noConst
from pypy.jit.metainterp.typesystem import LLTypeHelper, OOTypeHelper
from pypy.jit.metainterp.warmspot import get_stats
from pypy.jit.metainterp.warmstate import set_future_value
from pypy.rlib.jit import (JitDriver, we_are_jitted, hint, dont_look_inside,
    loop_invariant, elidable, promote, jit_debug, assert_green,
    AssertGreenFailed, unroll_safe, current_trace_length, look_inside_iff,
    isconstant, isvirtual, promote_string)
from pypy.rlib.rarithmetic import ovfcheck
from pypy.rpython.lltypesystem import lltype, llmemory, rffi
from pypy.rpython.ootypesystem import ootype


class BasicTests:
    def test_basic(self):
        def f(x, y):
            return x + y
        res = self.interp_operations(f, [40, 2])
        assert res == 42

    def test_basic_inst(self):
        class A:
            pass
        def f(n):
            a = A()
            a.x = n
            return a.x
        res = self.interp_operations(f, [42])
        assert res == 42

    def test_uint_floordiv(self):
        from pypy.rlib.rarithmetic import r_uint

        def f(a, b):
            a = r_uint(a)
            b = r_uint(b)
            return a/b

        res = self.interp_operations(f, [-4, 3])
        assert res == long(r_uint(-4)) // 3

    def test_direct_call(self):
        def g(n):
            return n + 2
        def f(a, b):
            return g(a) + g(b)
        res = self.interp_operations(f, [8, 98])
        assert res == 110

    def test_direct_call_with_guard(self):
        def g(n):
            if n < 0:
                return 0
            return n + 2
        def f(a, b):
            return g(a) + g(b)
        res = self.interp_operations(f, [8, 98])
        assert res == 110

    def test_loop(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'y', 'res'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += x
                y -= 1
            return res
        res = self.meta_interp(f, [6, 7])
        assert res == 42
        self.check_loop_count(1)
        self.check_loops({'guard_true': 1,
                          'int_add': 1, 'int_sub': 1, 'int_gt': 1,
                          'jump': 1})
        if self.basic:
            found = 0
            for op in get_stats().loops[0]._all_operations():
                if op.getopname() == 'guard_true':
                    liveboxes = op.getfailargs()
                    assert len(liveboxes) == 3
                    for box in liveboxes:
                        assert isinstance(box, history.BoxInt)
                    found += 1
            assert found == 1

    def test_loop_variant_mul1(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += x * x
                x += 1
                res += x * x
                y -= 1
            return res
        res = self.meta_interp(f, [6, 7])
        assert res == 1323
        self.check_loop_count(1)
        self.check_loops(int_mul=1)

    def test_loop_variant_mul_ovf(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += ovfcheck(x * x)
                x += 1
                res += ovfcheck(x * x)
                y -= 1
            return res
        res = self.meta_interp(f, [6, 7])
        assert res == 1323
        self.check_loop_count(1)
        self.check_loops(int_mul_ovf=1)

    def test_loop_invariant_mul1(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += x * x
                y -= 1
            return res
        res = self.meta_interp(f, [6, 7])
        assert res == 252
        self.check_loop_count(1)
        self.check_loops({'guard_true': 1,
                          'int_add': 1, 'int_sub': 1, 'int_gt': 1,
                          'jump': 1})

    def test_loop_invariant_mul_ovf(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                b = y * 2
                res += ovfcheck(x * x) + b
                y -= 1
            return res
        res = self.meta_interp(f, [6, 7])
        assert res == 308
        self.check_loop_count(1)
        self.check_loops({'guard_true': 1,
                          'int_add': 2, 'int_sub': 1, 'int_gt': 1,
                          'int_lshift': 1,
                          'jump': 1})

    def test_loop_invariant_mul_bridge1(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += x * x
                if y<16:
                    x += 1
                y -= 1
            return res
        res = self.meta_interp(f, [6, 32])
        assert res == 3427
        self.check_loop_count(3)

    def test_loop_invariant_mul_bridge_maintaining1(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += x * x
                if y<16:
                    res += 1
                y -= 1
            return res
        res = self.meta_interp(f, [6, 32])
        assert res == 1167
        self.check_loop_count(3)
        self.check_loops({'int_add': 3, 'int_lt': 2,
                          'int_sub': 2, 'guard_false': 1,
                          'jump': 2,
                          'int_gt': 1, 'guard_true': 2})


    def test_loop_invariant_mul_bridge_maintaining2(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                z = x * x
                res += z
                if y<16:
                    res += z
                y -= 1
            return res
        res = self.meta_interp(f, [6, 32])
        assert res == 1692
        self.check_loop_count(3)
        self.check_loops({'int_add': 3, 'int_lt': 2,
                          'int_sub': 2, 'guard_false': 1,
                          'jump': 2,
                          'int_gt': 1, 'guard_true': 2})

    def test_loop_invariant_mul_bridge_maintaining3(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x', 'm'])
        def f(x, y, m):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res, m=m)
                myjitdriver.jit_merge_point(x=x, y=y, res=res, m=m)
                z = x * x
                res += z
                if y<m:
                    res += z
                y -= 1
            return res
        res = self.meta_interp(f, [6, 32, 16])
        assert res == 1692
        self.check_loop_count(3)
        self.check_loops({'int_add': 2, 'int_lt': 1,
                          'int_sub': 2, 'guard_false': 1,
                          'jump': 2, 'int_mul': 1,
                          'int_gt': 2, 'guard_true': 2})

    def test_loop_invariant_intbox(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'res', 'x'])
        class I:
            __slots__ = 'intval'
            _immutable_ = True
            def __init__(self, intval):
                self.intval = intval
        def f(i, y):
            res = 0
            x = I(i)
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += x.intval * x.intval
                y -= 1
            return res
        res = self.meta_interp(f, [6, 7])
        assert res == 252
        self.check_loop_count(1)
        self.check_loops({'guard_true': 1,
                          'int_add': 1, 'int_sub': 1, 'int_gt': 1,
                          'jump': 1})

    def test_loops_are_transient(self):
        import gc, weakref
        myjitdriver = JitDriver(greens = [], reds = ['x', 'y', 'res'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += x
                if y%2:
                    res *= 2
                y -= 1
            return res
        wr_loops = []
        old_init = history.TreeLoop.__init__.im_func
        try:
            def track_init(self, name):
                old_init(self, name)
                wr_loops.append(weakref.ref(self))
            history.TreeLoop.__init__ = track_init
            res = self.meta_interp(f, [6, 15], no_stats=True)
        finally:
            history.TreeLoop.__init__ = old_init

        assert res == f(6, 15)
        gc.collect()

        #assert not [wr for wr in wr_loops if wr()]
        for loop in [wr for wr in wr_loops if wr()]:
            assert loop().name == 'short preamble'

    def test_string(self):
        def f(n):
            bytecode = 'adlfkj' + chr(n)
            if n < len(bytecode):
                return bytecode[n]
            else:
                return "?"
        res = self.interp_operations(f, [1])
        assert res == ord("d") # XXX should be "d"
        res = self.interp_operations(f, [6])
        assert res == 6
        res = self.interp_operations(f, [42])
        assert res == ord("?")

    def test_chr2str(self):
        def f(n):
            s = chr(n)
            return s[0]
        res = self.interp_operations(f, [3])
        assert res == 3

    def test_unicode(self):
        def f(n):
            bytecode = u'adlfkj' + unichr(n)
            if n < len(bytecode):
                return bytecode[n]
            else:
                return u"?"
        res = self.interp_operations(f, [1])
        assert res == ord(u"d") # XXX should be "d"
        res = self.interp_operations(f, [6])
        assert res == 6
        res = self.interp_operations(f, [42])
        assert res == ord(u"?")

    def test_residual_call(self):
        @dont_look_inside
        def externfn(x, y):
            return x * y
        def f(n):
            return externfn(n, n+1)
        res = self.interp_operations(f, [6])
        assert res == 42
        self.check_operations_history(int_add=1, int_mul=0, call=1, guard_no_exception=0)

    def test_residual_call_elidable(self):
        def externfn(x, y):
            return x * y
        externfn._elidable_function_ = True
        def f(n):
            promote(n)
            return externfn(n, n+1)
        res = self.interp_operations(f, [6])
        assert res == 42
        # CALL_PURE is not recorded in the history if all-constant args
        self.check_operations_history(int_add=0, int_mul=0,
                                      call=0, call_pure=0)

    def test_residual_call_elidable_1(self):
        @elidable
        def externfn(x, y):
            return x * y
        def f(n):
            return externfn(n, n+1)
        res = self.interp_operations(f, [6])
        assert res == 42
        # CALL_PURE is recorded in the history if not-all-constant args
        self.check_operations_history(int_add=1, int_mul=0,
                                      call=0, call_pure=1)

    def test_residual_call_elidable_2(self):
        myjitdriver = JitDriver(greens = [], reds = ['n'])
        @elidable
        def externfn(x):
            return x - 1
        def f(n):
            while n > 0:
                myjitdriver.can_enter_jit(n=n)
                myjitdriver.jit_merge_point(n=n)
                n = externfn(n)
            return n
        res = self.meta_interp(f, [7])
        assert res == 0
        # CALL_PURE is recorded in the history, but turned into a CALL
        # by optimizeopt.py
        self.check_loops(int_sub=0, call=1, call_pure=0)

    def test_constfold_call_elidable(self):
        myjitdriver = JitDriver(greens = ['m'], reds = ['n'])
        @elidable
        def externfn(x):
            return x - 3
        def f(n, m):
            while n > 0:
                myjitdriver.can_enter_jit(n=n, m=m)
                myjitdriver.jit_merge_point(n=n, m=m)
                n -= externfn(m)
            return n
        res = self.meta_interp(f, [21, 5])
        assert res == -1
        # the CALL_PURE is constant-folded away by optimizeopt.py
        self.check_loops(int_sub=1, call=0, call_pure=0)

    def test_constfold_call_elidable_2(self):
        myjitdriver = JitDriver(greens = ['m'], reds = ['n'])
        @elidable
        def externfn(x):
            return x - 3
        class V:
            def __init__(self, value):
                self.value = value
        def f(n, m):
            while n > 0:
                myjitdriver.can_enter_jit(n=n, m=m)
                myjitdriver.jit_merge_point(n=n, m=m)
                v = V(m)
                n -= externfn(v.value)
            return n
        res = self.meta_interp(f, [21, 5])
        assert res == -1
        # the CALL_PURE is constant-folded away by optimizeopt.py
        self.check_loops(int_sub=1, call=0, call_pure=0)

    def test_elidable_function_returning_object(self):
        myjitdriver = JitDriver(greens = ['m'], reds = ['n'])
        class V:
            def __init__(self, x):
                self.x = x
        v1 = V(1)
        v2 = V(2)
        @elidable
        def externfn(x):
            if x:
                return v1
            else:
                return v2
        def f(n, m):
            while n > 0:
                myjitdriver.can_enter_jit(n=n, m=m)
                myjitdriver.jit_merge_point(n=n, m=m)
                m = V(m).x
                n -= externfn(m).x + externfn(m + m - m).x
            return n
        res = self.meta_interp(f, [21, 5])
        assert res == -1
        # the CALL_PURE is constant-folded away by optimizeopt.py
        self.check_loops(int_sub=1, call=0, call_pure=0, getfield_gc=0)

    def test_elidable_raising(self):
        myjitdriver = JitDriver(greens = ['m'], reds = ['n'])
        @elidable
        def externfn(x):
            if x <= 0:
                raise ValueError
            return x - 1
        def f(n, m):
            while n > 0:
                myjitdriver.can_enter_jit(n=n, m=m)
                myjitdriver.jit_merge_point(n=n, m=m)
                try:
                    n -= externfn(m)
                except ValueError:
                    n -= 1
            return n
        res = self.meta_interp(f, [22, 6])
        assert res == -3
        # the CALL_PURE is constant-folded away during tracing
        self.check_loops(int_sub=1, call=0, call_pure=0)
        #
        res = self.meta_interp(f, [22, -5])
        assert res == 0
        # raises: becomes CALL and is not constant-folded away
        self.check_loops(int_sub=1, call=1, call_pure=0)

    def test_elidable_raising_2(self):
        myjitdriver = JitDriver(greens = ['m'], reds = ['n'])
        @elidable
        def externfn(x):
            if x <= 0:
                raise ValueError
            return x - 1
        def f(n, m):
            while n > 0:
                myjitdriver.can_enter_jit(n=n, m=m)
                myjitdriver.jit_merge_point(n=n, m=m)
                try:
                    n -= externfn(noConst(m))
                except ValueError:
                    n -= 1
            return n
        res = self.meta_interp(f, [22, 6])
        assert res == -3
        # the CALL_PURE is constant-folded away by optimizeopt.py
        self.check_loops(int_sub=1, call=0, call_pure=0)
        #
        res = self.meta_interp(f, [22, -5])
        assert res == 0
        # raises: becomes CALL and is not constant-folded away
        self.check_loops(int_sub=1, call=1, call_pure=0)

    def test_constant_across_mp(self):
        myjitdriver = JitDriver(greens = [], reds = ['n'])
        class X(object):
            pass
        def f(n):
            while n > -100:
                myjitdriver.can_enter_jit(n=n)
                myjitdriver.jit_merge_point(n=n)
                x = X()
                x.arg = 5
                if n <= 0: break
                n -= x.arg
                x.arg = 6   # prevents 'x.arg' from being annotated as constant
            return n
        res = self.meta_interp(f, [31])
        assert res == -4

    def test_stopatxpolicy(self):
        myjitdriver = JitDriver(greens = [], reds = ['y'])
        def internfn(y):
            return y * 3
        def externfn(y):
            return y % 4
        def f(y):
            while y >= 0:
                myjitdriver.can_enter_jit(y=y)
                myjitdriver.jit_merge_point(y=y)
                if y & 7:
                    f = internfn
                else:
                    f = externfn
                f(y)
                y -= 1
            return 42
        policy = StopAtXPolicy(externfn)
        res = self.meta_interp(f, [31], policy=policy)
        assert res == 42
        self.check_loops(int_mul=1, int_mod=0)

    def test_we_are_jitted(self):
        myjitdriver = JitDriver(greens = [], reds = ['y'])
        def f(y):
            while y >= 0:
                myjitdriver.can_enter_jit(y=y)
                myjitdriver.jit_merge_point(y=y)
                if we_are_jitted():
                    x = 1
                else:
                    x = 10
                y -= x
            return y
        assert f(55) == -5
        res = self.meta_interp(f, [55])
        assert res == -1

    def test_confirm_enter_jit(self):
        def confirm_enter_jit(x, y):
            return x <= 5
        myjitdriver = JitDriver(greens = ['x'], reds = ['y'],
                                confirm_enter_jit = confirm_enter_jit)
        def f(x, y):
            while y >= 0:
                myjitdriver.can_enter_jit(x=x, y=y)
                myjitdriver.jit_merge_point(x=x, y=y)
                y -= x
            return y
        #
        res = self.meta_interp(f, [10, 84])
        assert res == -6
        self.check_loop_count(0)
        #
        res = self.meta_interp(f, [3, 19])
        assert res == -2
        self.check_loop_count(1)

    def test_can_never_inline(self):
        def can_never_inline(x):
            return x > 50
        myjitdriver = JitDriver(greens = ['x'], reds = ['y'],
                                can_never_inline = can_never_inline)
        @dont_look_inside
        def marker():
            pass
        def f(x, y):
            while y >= 0:
                myjitdriver.can_enter_jit(x=x, y=y)
                myjitdriver.jit_merge_point(x=x, y=y)
                x += 1
                if x == 4 or x == 61:
                    marker()
                y -= x
            return y
        #
        res = self.meta_interp(f, [3, 6], repeat=7, function_threshold=0)
        assert res == 6 - 4 - 5
        self.check_history(call=0)   # because the trace starts in the middle
        #
        res = self.meta_interp(f, [60, 84], repeat=7)
        assert res == 84 - 61 - 62
        self.check_history(call=1)   # because the trace starts immediately

    def test_unroll_one_loop_iteration(self):
        def unroll(code):
            return code == 0
        myjitdriver = JitDriver(greens = ['code'],
                                reds = ['loops', 'inner_loops', 's'],
                                should_unroll_one_iteration=unroll)

        def f(code, loops, inner_loops):
            s = 0
            while loops > 0:
                myjitdriver.jit_merge_point(code=code, loops=loops,
                                            inner_loops=inner_loops, s=s)
                if code == 1:
                    s += f(0, inner_loops, 0)
                loops -= 1
                s += 1
            return s

        res = self.meta_interp(f, [1, 4, 1], enable_opts="", inline=True)
        assert res == f(1, 4, 1)
        self.check_history(call_assembler=0)

        res = self.meta_interp(f, [1, 4, 2], enable_opts="", inline=True)
        assert res == f(1, 4, 2)
        self.check_history(call_assembler=1)

    def test_format(self):
        def f(n):
            return len("<%d>" % n)
        res = self.interp_operations(f, [421])
        assert res == 5

    def test_switch(self):
        def f(n):
            if n == -5:  return 12
            elif n == 2: return 51
            elif n == 7: return 1212
            else:        return 42
        res = self.interp_operations(f, [7])
        assert res == 1212
        res = self.interp_operations(f, [12311])
        assert res == 42

    def test_r_uint(self):
        from pypy.rlib.rarithmetic import r_uint
        myjitdriver = JitDriver(greens = [], reds = ['y'])
        def f(y):
            y = r_uint(y)
            while y > 0:
                myjitdriver.can_enter_jit(y=y)
                myjitdriver.jit_merge_point(y=y)
                y -= 1
            return y
        res = self.meta_interp(f, [10])
        assert res == 0

    def test_uint_operations(self):
        from pypy.rlib.rarithmetic import r_uint
        def f(n):
            return ((r_uint(n) - 123) >> 1) <= r_uint(456)
        res = self.interp_operations(f, [50])
        assert res == False
        self.check_operations_history(int_rshift=0, uint_rshift=1,
                                      int_le=0, uint_le=1,
                                      int_sub=1)

    def test_uint_condition(self):
        from pypy.rlib.rarithmetic import r_uint
        def f(n):
            if ((r_uint(n) - 123) >> 1) <= r_uint(456):
                return 24
            else:
                return 12
        res = self.interp_operations(f, [50])
        assert res == 12
        self.check_operations_history(int_rshift=0, uint_rshift=1,
                                      int_le=0, uint_le=1,
                                      int_sub=1)

    def test_int_between(self):
        #
        def check(arg1, arg2, arg3, expect_result, **expect_operations):
            from pypy.rpython.lltypesystem import lltype
            from pypy.rpython.lltypesystem.lloperation import llop
            loc = locals().copy()
            exec py.code.Source("""
                def f(n, m, p):
                    arg1 = %(arg1)s
                    arg2 = %(arg2)s
                    arg3 = %(arg3)s
                    return llop.int_between(lltype.Bool, arg1, arg2, arg3)
            """ % locals()).compile() in loc
            res = self.interp_operations(loc['f'], [5, 6, 7])
            assert res == expect_result
            self.check_operations_history(expect_operations)
        #
        check('n', 'm', 'p', True,  int_sub=2, uint_lt=1)
        check('n', 'p', 'm', False, int_sub=2, uint_lt=1)
        #
        check('n', 'm', 6, False, int_sub=2, uint_lt=1)
        #
        check('n', 4, 'p', False, int_sub=2, uint_lt=1)
        check('n', 5, 'p', True,  int_sub=2, uint_lt=1)
        check('n', 8, 'p', False, int_sub=2, uint_lt=1)
        #
        check('n', 6, 7, True, int_sub=2, uint_lt=1)
        #
        check(-2, 'n', 'p', True,  int_sub=2, uint_lt=1)
        check(-2, 'm', 'p', True,  int_sub=2, uint_lt=1)
        check(-2, 'p', 'm', False, int_sub=2, uint_lt=1)
        #check(0, 'n', 'p', True,  uint_lt=1)   xxx implement me
        #check(0, 'm', 'p', True,  uint_lt=1)
        #check(0, 'p', 'm', False, uint_lt=1)
        #
        check(2, 'n', 6, True,  int_sub=1, uint_lt=1)
        check(2, 'm', 6, False, int_sub=1, uint_lt=1)
        check(2, 'p', 6, False, int_sub=1, uint_lt=1)
        check(5, 'n', 6, True,  int_eq=1)    # 6 == 5+1
        check(5, 'm', 6, False, int_eq=1)    # 6 == 5+1
        #
        check(2, 6, 'm', False, int_sub=1, uint_lt=1)
        check(2, 6, 'p', True,  int_sub=1, uint_lt=1)
        #
        check(2, 40, 6,  False)
        check(2, 40, 60, True)

    def test_getfield(self):
        class A:
            pass
        a1 = A()
        a1.foo = 5
        a2 = A()
        a2.foo = 8
        def f(x):
            if x > 5:
                a = a1
            else:
                a = a2
            return a.foo * x
        res = self.interp_operations(f, [42])
        assert res == 210
        self.check_operations_history(getfield_gc=1)

    def test_getfield_immutable(self):
        class A:
            _immutable_ = True
        a1 = A()
        a1.foo = 5
        a2 = A()
        a2.foo = 8
        def f(x):
            if x > 5:
                a = a1
            else:
                a = a2
            return a.foo * x
        res = self.interp_operations(f, [42])
        assert res == 210
        self.check_operations_history(getfield_gc=0)

    def test_setfield_bool(self):
        class A:
            def __init__(self):
                self.flag = True
        myjitdriver = JitDriver(greens = [], reds = ['n', 'obj'])
        def f(n):
            obj = A()
            res = False
            while n > 0:
                myjitdriver.can_enter_jit(n=n, obj=obj)
                myjitdriver.jit_merge_point(n=n, obj=obj)
                obj.flag = False
                n -= 1
            return res
        res = self.meta_interp(f, [7])
        assert type(res) == bool
        assert not res

    def test_switch_dict(self):
        def f(x):
            if   x == 1: return 61
            elif x == 2: return 511
            elif x == 3: return -22
            elif x == 4: return 81
            elif x == 5: return 17
            elif x == 6: return 54
            elif x == 7: return 987
            elif x == 8: return -12
            elif x == 9: return 321
            return -1
        res = self.interp_operations(f, [5])
        assert res == 17
        res = self.interp_operations(f, [15])
        assert res == -1

    def test_int_add_ovf(self):
        def f(x, y):
            try:
                return ovfcheck(x + y)
            except OverflowError:
                return -42
        res = self.interp_operations(f, [-100, 2])
        assert res == -98
        res = self.interp_operations(f, [1, sys.maxint])
        assert res == -42

    def test_int_sub_ovf(self):
        def f(x, y):
            try:
                return ovfcheck(x - y)
            except OverflowError:
                return -42
        res = self.interp_operations(f, [-100, 2])
        assert res == -102
        res = self.interp_operations(f, [1, -sys.maxint])
        assert res == -42

    def test_int_mul_ovf(self):
        def f(x, y):
            try:
                return ovfcheck(x * y)
            except OverflowError:
                return -42
        res = self.interp_operations(f, [-100, 2])
        assert res == -200
        res = self.interp_operations(f, [-3, sys.maxint//2])
        assert res == -42

    def test_mod_ovf(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'x', 'y'])
        def f(n, x, y):
            while n > 0:
                myjitdriver.can_enter_jit(x=x, y=y, n=n)
                myjitdriver.jit_merge_point(x=x, y=y, n=n)
                n -= ovfcheck(x % y)
            return n
        res = self.meta_interp(f, [20, 1, 2])
        assert res == 0
        self.check_loops(call=0)

    def test_abs(self):
        myjitdriver = JitDriver(greens = [], reds = ['i', 't'])
        def f(i):
            t = 0
            while i < 10:
                myjitdriver.can_enter_jit(i=i, t=t)
                myjitdriver.jit_merge_point(i=i, t=t)
                t += abs(i)
                i += 1
            return t
        res = self.meta_interp(f, [-5])
        assert res == 5+4+3+2+1+0+1+2+3+4+5+6+7+8+9

    def test_float(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'y', 'res'])
        def f(x, y):
            x = float(x)
            y = float(y)
            res = 0.0
            while y > 0.0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                res += x
                y -= 1.0
            return res
        res = self.meta_interp(f, [6, 7])
        assert res == 42.0
        self.check_loop_count(1)
        self.check_loops({'guard_true': 1,
                          'float_add': 1, 'float_sub': 1, 'float_gt': 1,
                          'jump': 1})

    def test_print(self):
        myjitdriver = JitDriver(greens = [], reds = ['n'])
        def f(n):
            while n > 0:
                myjitdriver.can_enter_jit(n=n)
                myjitdriver.jit_merge_point(n=n)
                print n
                n -= 1
            return n
        res = self.meta_interp(f, [7])
        assert res == 0

    def test_bridge_from_interpreter(self):
        mydriver = JitDriver(reds = ['n'], greens = [])

        def f(n):
            while n > 0:
                mydriver.can_enter_jit(n=n)
                mydriver.jit_merge_point(n=n)
                n -= 1

        self.meta_interp(f, [20], repeat=7)
        self.check_tree_loop_count(2)      # the loop and the entry path
        # we get:
        #    ENTER             - compile the new loop and the entry bridge
        #    ENTER             - compile the leaving path
        self.check_enter_count(2)

    def test_bridge_from_interpreter_2(self):
        # one case for backend - computing of framesize on guard failure
        mydriver = JitDriver(reds = ['n'], greens = [])
        glob = [1]

        def f(n):
            while n > 0:
                mydriver.can_enter_jit(n=n)
                mydriver.jit_merge_point(n=n)
                if n == 17 and glob[0]:
                    glob[0] = 0
                    x = n + 1
                    y = n + 2
                    z = n + 3
                    k = n + 4
                    n -= 1
                    n += x + y + z + k
                    n -= x + y + z + k
                n -= 1

        self.meta_interp(f, [20], repeat=7)

    def test_bridge_from_interpreter_3(self):
        # one case for backend - computing of framesize on guard failure
        mydriver = JitDriver(reds = ['n', 'x', 'y', 'z', 'k'], greens = [])
        class Global:
            pass
        glob = Global()

        def f(n):
            glob.x = 1
            x = 0
            y = 0
            z = 0
            k = 0
            while n > 0:
                mydriver.can_enter_jit(n=n, x=x, y=y, z=z, k=k)
                mydriver.jit_merge_point(n=n, x=x, y=y, z=z, k=k)
                x += 10
                y += 3
                z -= 15
                k += 4
                if n == 17 and glob.x:
                    glob.x = 0
                    x += n + 1
                    y += n + 2
                    z += n + 3
                    k += n + 4
                    n -= 1
                n -= 1
            return x + 2*y + 3*z + 5*k + 13*n

        res = self.meta_interp(f, [20], repeat=7)
        assert res == f(20)

    def test_bridge_from_interpreter_4(self):
        jitdriver = JitDriver(reds = ['n', 'k'], greens = [])

        def f(n, k):
            while n > 0:
                jitdriver.can_enter_jit(n=n, k=k)
                jitdriver.jit_merge_point(n=n, k=k)
                if k:
                    n -= 2
                else:
                    n -= 1
            return n + k

        from pypy.rpython.test.test_llinterp import get_interpreter, clear_tcache
        from pypy.jit.metainterp.warmspot import WarmRunnerDesc

        interp, graph = get_interpreter(f, [0, 0], backendopt=False,
                                        inline_threshold=0, type_system=self.type_system)
        clear_tcache()
        translator = interp.typer.annotator.translator
        translator.config.translation.gc = "boehm"
        warmrunnerdesc = WarmRunnerDesc(translator,
                                        CPUClass=self.CPUClass)
        state = warmrunnerdesc.jitdrivers_sd[0].warmstate
        state.set_param_threshold(3)          # for tests
        state.set_param_trace_eagerness(0)    # for tests
        warmrunnerdesc.finish()
        for n, k in [(20, 0), (20, 1)]:
            interp.eval_graph(graph, [n, k])

    def test_bridge_leaving_interpreter_5(self):
        mydriver = JitDriver(reds = ['n', 'x'], greens = [])
        class Global:
            pass
        glob = Global()

        def f(n):
            x = 0
            glob.x = 1
            while n > 0:
                mydriver.can_enter_jit(n=n, x=x)
                mydriver.jit_merge_point(n=n, x=x)
                glob.x += 1
                x += 3
                n -= 1
            glob.x += 100
            return glob.x + x
        res = self.meta_interp(f, [20], repeat=7)
        assert res == f(20)

    def test_instantiate_classes(self):
        class Base: pass
        class A(Base): foo = 72
        class B(Base): foo = 8
        def f(n):
            if n > 5:
                cls = A
            else:
                cls = B
            return cls().foo
        res = self.interp_operations(f, [3])
        assert res == 8
        res = self.interp_operations(f, [13])
        assert res == 72

    def test_instantiate_does_not_call(self):
        mydriver = JitDriver(reds = ['n', 'x'], greens = [])
        class Base: pass
        class A(Base): foo = 72
        class B(Base): foo = 8

        def f(n):
            x = 0
            while n > 0:
                mydriver.can_enter_jit(n=n, x=x)
                mydriver.jit_merge_point(n=n, x=x)
                if n % 2 == 0:
                    cls = A
                else:
                    cls = B
                inst = cls()
                x += inst.foo
                n -= 1
            return x
        res = self.meta_interp(f, [20], enable_opts='')
        assert res == f(20)
        self.check_loops(call=0)

    def test_zerodivisionerror(self):
        # test the case of exception-raising operation that is not delegated
        # to the backend at all: ZeroDivisionError
        #
        def f(n):
            assert n >= 0
            try:
                return ovfcheck(5 % n)
            except ZeroDivisionError:
                return -666
            except OverflowError:
                return -777
        res = self.interp_operations(f, [0])
        assert res == -666
        #
        def f(n):
            assert n >= 0
            try:
                return ovfcheck(6 // n)
            except ZeroDivisionError:
                return -667
            except OverflowError:
                return -778
        res = self.interp_operations(f, [0])
        assert res == -667

    def test_div_overflow(self):
        import sys
        from pypy.rpython.lltypesystem.lloperation import llop
        myjitdriver = JitDriver(greens = [], reds = ['x', 'y', 'res'])
        def f(x, y):
            res = 0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                try:
                    res += llop.int_floordiv_ovf(lltype.Signed,
                                                 -sys.maxint-1, x)
                    x += 5
                except OverflowError:
                    res += 100
                y -= 1
            return res
        res = self.meta_interp(f, [-41, 16])
        assert res == ((-sys.maxint-1) // (-41) +
                       (-sys.maxint-1) // (-36) +
                       (-sys.maxint-1) // (-31) +
                       (-sys.maxint-1) // (-26) +
                       (-sys.maxint-1) // (-21) +
                       (-sys.maxint-1) // (-16) +
                       (-sys.maxint-1) // (-11) +
                       (-sys.maxint-1) // (-6) +
                       100 * 8)

    def test_isinstance(self):
        class A:
            pass
        class B(A):
            pass
        @dont_look_inside
        def extern(n):
            if n:
                return A()
            else:
                return B()
        def fn(n):
            obj = extern(n)
            return isinstance(obj, B)
        res = self.interp_operations(fn, [0])
        assert res
        self.check_operations_history(guard_class=1)
        res = self.interp_operations(fn, [1])
        assert not res

    def test_isinstance_2(self):
        driver = JitDriver(greens = [], reds = ['n', 'sum', 'x'])
        class A:
            pass
        class B(A):
            pass
        class C(B):
            pass

        def main():
            return f(5, B()) * 10 + f(5, C()) + f(5, A()) * 100

        def f(n, x):
            sum = 0
            while n > 0:
                driver.can_enter_jit(x=x, n=n, sum=sum)
                driver.jit_merge_point(x=x, n=n, sum=sum)
                if isinstance(x, B):
                    sum += 1
                n -= 1
            return sum

        res = self.meta_interp(main, [])
        assert res == 55


    def test_assert_isinstance(self):
        class A:
            pass
        class B(A):
            pass
        def fn(n):
            # this should only be called with n != 0
            if n:
                obj = B()
                obj.a = n
            else:
                obj = A()
                obj.a = 17
            assert isinstance(obj, B)
            return obj.a
        res = self.interp_operations(fn, [1])
        assert res == 1
        self.check_operations_history(guard_class=0)
        if self.type_system == 'ootype':
            self.check_operations_history(instanceof=0)

    def test_r_dict(self):
        from pypy.rlib.objectmodel import r_dict
        class FooError(Exception):
            pass
        def myeq(n, m):
            return n == m
        def myhash(n):
            if n < 0:
                raise FooError
            return -n
        def f(n):
            d = r_dict(myeq, myhash)
            for i in range(10):
                d[i] = i*i
            try:
                return d[n]
            except FooError:
                return 99
        res = self.interp_operations(f, [5])
        assert res == f(5)

    def test_free_object(self):
        import weakref
        from pypy.rlib import rgc
        from pypy.rpython.lltypesystem.lloperation import llop
        myjitdriver = JitDriver(greens = [], reds = ['n', 'x'])
        class X(object):
            pass
        def main(n, x):
            while n > 0:
                myjitdriver.can_enter_jit(n=n, x=x)
                myjitdriver.jit_merge_point(n=n, x=x)
                n -= x.foo
        def g(n):
            x = X()
            x.foo = 2
            main(n, x)
            x.foo = 5
            return weakref.ref(x)
        def f(n):
            r = g(n)
            rgc.collect(); rgc.collect(); rgc.collect()
            return r() is None
        #
        assert f(30) == 1
        res = self.meta_interp(f, [30], no_stats=True)
        assert res == 1

    def test_pass_around(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'x'])

        def call():
            pass

        def f(n, x):
            while n > 0:
                myjitdriver.can_enter_jit(n=n, x=x)
                myjitdriver.jit_merge_point(n=n, x=x)
                if n % 2:
                    call()
                    if n == 8:
                        return x
                    x = 3
                else:
                    x = 5
                n -= 1
            return 0

        self.meta_interp(f, [40, 0])

    def test_const_inputargs(self):
        myjitdriver = JitDriver(greens = ['m'], reds = ['n', 'x'])
        def f(n, x):
            m = 0x7FFFFFFF
            while n > 0:
                myjitdriver.can_enter_jit(m=m, n=n, x=x)
                myjitdriver.jit_merge_point(m=m, n=n, x=x)
                x = 42
                n -= 1
                m = m >> 1
            return x

        res = self.meta_interp(f, [50, 1], enable_opts='')
        assert res == 42

    def test_set_param(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'x'])
        def g(n):
            x = 0
            while n > 0:
                myjitdriver.can_enter_jit(n=n, x=x)
                myjitdriver.jit_merge_point(n=n, x=x)
                n -= 1
                x += n
            return x
        def f(n, threshold):
            myjitdriver.set_param('threshold', threshold)
            return g(n)

        res = self.meta_interp(f, [10, 3])
        assert res == 9 + 8 + 7 + 6 + 5 + 4 + 3 + 2 + 1 + 0
        self.check_tree_loop_count(2)

        res = self.meta_interp(f, [10, 13])
        assert res == 9 + 8 + 7 + 6 + 5 + 4 + 3 + 2 + 1 + 0
        self.check_tree_loop_count(0)

    def test_dont_look_inside(self):
        @dont_look_inside
        def g(a, b):
            return a + b
        def f(a, b):
            return g(a, b)
        res = self.interp_operations(f, [3, 5])
        assert res == 8
        self.check_operations_history(int_add=0, call=1)

    def test_listcomp(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'y', 'lst'])
        def f(x, y):
            lst = [0, 0, 0]
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, lst=lst)
                myjitdriver.jit_merge_point(x=x, y=y, lst=lst)
                lst = [i+x for i in lst if i >=0]
                y -= 1
            return lst[0]
        res = self.meta_interp(f, [6, 7], listcomp=True, backendopt=True, listops=True)
        # XXX: the loop looks inefficient
        assert res == 42

    def test_tuple_immutable(self):
        def new(a, b):
            return a, b
        def f(a, b):
            tup = new(a, b)
            return tup[1]
        res = self.interp_operations(f, [3, 5])
        assert res == 5
        self.check_operations_history(setfield_gc=2, getfield_gc_pure=0)

    def test_oosend_look_inside_only_one(self):
        class A:
            pass
        class B(A):
            def g(self):
                return 123
        class C(A):
            @dont_look_inside
            def g(self):
                return 456
        def f(n):
            if n > 3:
                x = B()
            else:
                x = C()
            return x.g() + x.g()
        res = self.interp_operations(f, [10])
        assert res == 123 * 2
        res = self.interp_operations(f, [-10])
        assert res == 456 * 2

    def test_residual_external_call(self):
        import math
        myjitdriver = JitDriver(greens = [], reds = ['y', 'x', 'res'])

        # When this test was written ll_math couldn't be inlined, now it can,
        # instead of rewriting this test, just ensure that an external call is
        # still generated by wrapping the function.
        @dont_look_inside
        def modf(x):
            return math.modf(x)

        def f(x, y):
            x = float(x)
            res = 0.0
            while y > 0:
                myjitdriver.can_enter_jit(x=x, y=y, res=res)
                myjitdriver.jit_merge_point(x=x, y=y, res=res)
                # this is an external call that the default policy ignores
                rpart, ipart = modf(x)
                res += ipart
                y -= 1
            return res
        res = self.meta_interp(f, [6, 7])
        assert res == 42
        self.check_loop_count(1)
        self.check_loops(call=1)

    def test_merge_guardclass_guardvalue(self):
        from pypy.rlib.objectmodel import instantiate
        myjitdriver = JitDriver(greens = [], reds = ['x', 'l'])

        class A(object):
            def g(self, x):
                return x - 5
        class B(A):
            def g(self, y):
                return y - 3

        a1 = A()
        a2 = A()
        b = B()
        def f(x):
            l = [a1] * 100 + [a2] * 100 + [b] * 100
            while x > 0:
                myjitdriver.can_enter_jit(x=x, l=l)
                myjitdriver.jit_merge_point(x=x, l=l)
                a = l[x]
                x = a.g(x)
                promote(a)
            return x
        res = self.meta_interp(f, [299], listops=True)
        assert res == f(299)
        self.check_loops(guard_class=0, guard_value=3)
        self.check_loops(guard_class=0, guard_value=6, everywhere=True)

    def test_merge_guardnonnull_guardclass(self):
        from pypy.rlib.objectmodel import instantiate
        myjitdriver = JitDriver(greens = [], reds = ['x', 'l'])

        class A(object):
            def g(self, x):
                return x - 3
        class B(A):
            def g(self, y):
                return y - 5

        a1 = A()
        b1 = B()
        def f(x):
            l = [None] * 100 + [b1] * 100 + [a1] * 100
            while x > 0:
                myjitdriver.can_enter_jit(x=x, l=l)
                myjitdriver.jit_merge_point(x=x, l=l)
                a = l[x]
                if a:
                    x = a.g(x)
                else:
                    x -= 7
            return x
        res = self.meta_interp(f, [299], listops=True)
        assert res == f(299)
        self.check_loops(guard_class=0, guard_nonnull=2,
                         guard_nonnull_class=2, guard_isnull=1)
        self.check_loops(guard_class=0, guard_nonnull=4,
                         guard_nonnull_class=4, guard_isnull=2,
                         everywhere=True)

    def test_merge_guardnonnull_guardvalue(self):
        from pypy.rlib.objectmodel import instantiate
        myjitdriver = JitDriver(greens = [], reds = ['x', 'l'])

        class A(object):
            pass
        class B(A):
            pass

        a1 = A()
        b1 = B()
        def f(x):
            l = [b1] * 100 + [None] * 100 + [a1] * 100
            while x > 0:
                myjitdriver.can_enter_jit(x=x, l=l)
                myjitdriver.jit_merge_point(x=x, l=l)
                a = l[x]
                if a:
                    x -= 5
                else:
                    x -= 7
                promote(a)
            return x
        res = self.meta_interp(f, [299], listops=True)
        assert res == f(299)
        self.check_loops(guard_class=0, guard_nonnull=2, guard_value=2,
                         guard_nonnull_class=0, guard_isnull=1)
        self.check_loops(guard_class=0, guard_nonnull=4, guard_value=4,
                         guard_nonnull_class=0, guard_isnull=2,
                         everywhere=True)

    def test_merge_guardnonnull_guardvalue_2(self):
        from pypy.rlib.objectmodel import instantiate
        myjitdriver = JitDriver(greens = [], reds = ['x', 'l'])

        class A(object):
            pass
        class B(A):
            pass

        a1 = A()
        b1 = B()
        def f(x):
            l = [None] * 100 + [b1] * 100 + [a1] * 100
            while x > 0:
                myjitdriver.can_enter_jit(x=x, l=l)
                myjitdriver.jit_merge_point(x=x, l=l)
                a = l[x]
                if a:
                    x -= 5
                else:
                    x -= 7
                promote(a)
            return x
        res = self.meta_interp(f, [299], listops=True)
        assert res == f(299)
        self.check_loops(guard_class=0, guard_nonnull=2, guard_value=2,
                         guard_nonnull_class=0, guard_isnull=1)
        self.check_loops(guard_class=0, guard_nonnull=4, guard_value=4,
                         guard_nonnull_class=0, guard_isnull=2,
                         everywhere=True)

    def test_merge_guardnonnull_guardclass_guardvalue(self):
        from pypy.rlib.objectmodel import instantiate
        myjitdriver = JitDriver(greens = [], reds = ['x', 'l'])

        class A(object):
            def g(self, x):
                return x - 3
        class B(A):
            def g(self, y):
                return y - 5

        a1 = A()
        a2 = A()
        b1 = B()
        def f(x):
            l = [a2] * 100 + [None] * 100 + [b1] * 100 + [a1] * 100
            while x > 0:
                myjitdriver.can_enter_jit(x=x, l=l)
                myjitdriver.jit_merge_point(x=x, l=l)
                a = l[x]
                if a:
                    x = a.g(x)
                else:
                    x -= 7
                promote(a)
            return x
        res = self.meta_interp(f, [399], listops=True)
        assert res == f(399)
        self.check_loops(guard_class=0, guard_nonnull=3, guard_value=3,
                         guard_nonnull_class=0, guard_isnull=1)
        self.check_loops(guard_class=0, guard_nonnull=6, guard_value=6,
                         guard_nonnull_class=0, guard_isnull=2,
                         everywhere=True)

    def test_residual_call_doesnt_lose_info(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'y', 'l'])

        class A(object):
            pass

        globall = [""]
        @dont_look_inside
        def g(x):
            globall[0] = str(x)
            return x

        def f(x):
            y = A()
            y.v = x
            l = [0]
            while y.v > 0:
                myjitdriver.can_enter_jit(x=x, y=y, l=l)
                myjitdriver.jit_merge_point(x=x, y=y, l=l)
                l[0] = y.v
                lc = l[0]
                y.v = g(y.v) - y.v/y.v + lc/l[0] - 1
            return y.v
        res = self.meta_interp(f, [20], listops=True)
        self.check_loops(getfield_gc=0, getarrayitem_gc=0)
        self.check_loops(getfield_gc=1, getarrayitem_gc=0, everywhere=True)

    def test_guard_isnull_nonnull(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'res'])
        class A(object):
            pass

        @dont_look_inside
        def create(x):
            if x >= -40:
                return A()
            return None

        def f(x):
            res = 0
            while x > 0:
                myjitdriver.can_enter_jit(x=x, res=res)
                myjitdriver.jit_merge_point(x=x, res=res)
                obj = create(x-1)
                if obj is not None:
                    res += 1
                obj2 = create(x-1000)
                if obj2 is None:
                    res += 1
                x -= 1
            return res
        res = self.meta_interp(f, [21])
        assert res == 42
        self.check_loops(guard_nonnull=1, guard_isnull=1)

    def test_loop_invariant1(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'res'])
        class A(object):
            pass
        a = A()
        a.current_a = A()
        a.current_a.x = 1
        @loop_invariant
        def f():
            return a.current_a

        def g(x):
            res = 0
            while x > 0:
                myjitdriver.can_enter_jit(x=x, res=res)
                myjitdriver.jit_merge_point(x=x, res=res)
                res += f().x
                res += f().x
                res += f().x
                x -= 1
            a.current_a = A()
            a.current_a.x = 2
            return res
        res = self.meta_interp(g, [21])
        assert res == 3 * 21
        self.check_loops(call=0)
        self.check_loops(call=1, everywhere=True)

    def test_bug_optimizeopt_mutates_ops(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'res', 'const', 'a'])
        class A(object):
            pass
        class B(A):
            pass

        glob = A()
        glob.a = None
        def f(x):
            res = 0
            a = A()
            a.x = 0
            glob.a = A()
            const = 2
            while x > 0:
                myjitdriver.can_enter_jit(x=x, res=res, a=a, const=const)
                myjitdriver.jit_merge_point(x=x, res=res, a=a, const=const)
                if type(glob.a) is B:
                    res += 1
                if a is None:
                    a = A()
                    a.x = x
                    glob.a = B()
                    const = 2
                else:
                    promote(const)
                    x -= const
                    res += a.x
                    a = None
                    glob.a = A()
                    const = 1
            return res
        res = self.meta_interp(f, [21])
        assert res == f(21)

    def test_getitem_indexerror(self):
        lst = [10, 4, 9, 16]
        def f(n):
            try:
                return lst[n]
            except IndexError:
                return -2
        res = self.interp_operations(f, [2])
        assert res == 9
        res = self.interp_operations(f, [4])
        assert res == -2
        res = self.interp_operations(f, [-4])
        assert res == 10
        res = self.interp_operations(f, [-5])
        assert res == -2

    def test_guard_always_changing_value(self):
        myjitdriver = JitDriver(greens = [], reds = ['x'])
        class A:
            pass
        def f(x):
            while x > 0:
                myjitdriver.can_enter_jit(x=x)
                myjitdriver.jit_merge_point(x=x)
                a = A()
                promote(a)
                x -= 1
        self.meta_interp(f, [50])
        self.check_loop_count(1)
        # this checks that the logic triggered by make_a_counter_per_value()
        # works and prevents generating tons of bridges

    def test_swap_values(self):
        def f(x, y):
            if x > 5:
                x, y = y, x
            return x - y
        res = self.interp_operations(f, [10, 2])
        assert res == -8
        res = self.interp_operations(f, [3, 2])
        assert res == 1

    def test_raw_malloc_and_access(self):
        TP = rffi.CArray(lltype.Signed)

        def f(n):
            a = lltype.malloc(TP, n, flavor='raw')
            a[0] = n
            res = a[0]
            lltype.free(a, flavor='raw')
            return res

        res = self.interp_operations(f, [10])
        assert res == 10

    def test_raw_malloc_and_access_float(self):
        TP = rffi.CArray(lltype.Float)

        def f(n, f):
            a = lltype.malloc(TP, n, flavor='raw')
            a[0] = f
            res = a[0]
            lltype.free(a, flavor='raw')
            return res

        res = self.interp_operations(f, [10, 3.5])
        assert res == 3.5

    def test_jit_debug(self):
        myjitdriver = JitDriver(greens = [], reds = ['x'])
        class A:
            pass
        def f(x):
            while x > 0:
                myjitdriver.can_enter_jit(x=x)
                myjitdriver.jit_merge_point(x=x)
                jit_debug("hi there:", x)
                jit_debug("foobar")
                x -= 1
            return x
        res = self.meta_interp(f, [8])
        assert res == 0
        self.check_loops(jit_debug=2)

    def test_assert_green(self):
        def f(x, promote_flag):
            if promote_flag:
                promote(x)
            assert_green(x)
            return x
        res = self.interp_operations(f, [8, 1])
        assert res == 8
        py.test.raises(AssertGreenFailed, self.interp_operations, f, [8, 0])

    def test_multiple_specialied_versions1(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'x', 'res'])
        class Base:
            def __init__(self, val):
                self.val = val
        class A(Base):
            def binop(self, other):
                return A(self.val + other.val)
        class B(Base):
            def binop(self, other):
                return B(self.val * other.val)
        def f(x, y):
            res = x
            while y > 0:
                myjitdriver.can_enter_jit(y=y, x=x, res=res)
                myjitdriver.jit_merge_point(y=y, x=x, res=res)
                res = res.binop(x)
                y -= 1
            return res
        def g(x, y):
            a1 = f(A(x), y)
            a2 = f(A(x), y)
            b1 = f(B(x), y)
            b2 = f(B(x), y)
            assert a1.val == a2.val
            assert b1.val == b2.val
            return a1.val + b1.val
        res = self.meta_interp(g, [6, 7])
        assert res == 6*8 + 6**8
        self.check_loop_count(5)
        self.check_loops({'guard_true': 2,
                          'int_add': 1, 'int_mul': 1, 'int_sub': 2,
                          'int_gt': 2, 'jump': 2})

    def test_multiple_specialied_versions_array(self):
        myjitdriver = JitDriver(greens = [], reds = ['idx', 'y', 'x', 'res',
                                                     'array'])
        class Base:
            def __init__(self, val):
                self.val = val
        class A(Base):
            def binop(self, other):
                return A(self.val + other.val)
        class B(Base):
            def binop(self, other):
                return B(self.val - other.val)
        def f(x, y):
            res = x
            array = [1, 2, 3]
            array[1] = 7
            idx = 0
            while y > 0:
                myjitdriver.can_enter_jit(idx=idx, y=y, x=x, res=res,
                                          array=array)
                myjitdriver.jit_merge_point(idx=idx, y=y, x=x, res=res,
                                            array=array)
                res = res.binop(x)
                res.val += array[idx] + array[1]
                if y < 7:
                    idx = 2
                y -= 1
            return res
        def g(x, y):
            a1 = f(A(x), y)
            a2 = f(A(x), y)
            b1 = f(B(x), y)
            b2 = f(B(x), y)
            assert a1.val == a2.val
            assert b1.val == b2.val
            return a1.val + b1.val
        res = self.meta_interp(g, [6, 14])
        assert res == g(6, 14)
        self.check_loop_count(9)
        self.check_loops(getarrayitem_gc=8, everywhere=True)

    def test_multiple_specialied_versions_bridge(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'x', 'z', 'res'])
        class Base:
            def __init__(self, val):
                self.val = val
            def getval(self):
                return self.val
        class A(Base):
            def binop(self, other):
                return A(self.getval() + other.getval())
        class B(Base):
            def binop(self, other):
                return B(self.getval() * other.getval())
        def f(x, y, z):
            res = x
            while y > 0:
                myjitdriver.can_enter_jit(y=y, x=x, z=z, res=res)
                myjitdriver.jit_merge_point(y=y, x=x, z=z, res=res)
                res = res.binop(x)
                y -= 1
                if y < 7:
                    x = z
            return res
        def g(x, y):
            a1 = f(A(x), y, A(x))
            a2 = f(A(x), y, A(x))
            assert a1.val == a2.val
            b1 = f(B(x), y, B(x))
            b2 = f(B(x), y, B(x))
            assert b1.val == b2.val
            c1 = f(B(x), y, A(x))
            c2 = f(B(x), y, A(x))
            assert c1.val == c2.val
            d1 = f(A(x), y, B(x))
            d2 = f(A(x), y, B(x))
            assert d1.val == d2.val
            return a1.val + b1.val + c1.val + d1.val
        res = self.meta_interp(g, [3, 14])
        assert res == g(3, 14)

    def test_failing_inlined_guard(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'x', 'z', 'res'])
        class Base:
            def __init__(self, val):
                self.val = val
            def getval(self):
                return self.val
        class A(Base):
            def binop(self, other):
                return A(self.getval() + other.getval())
        class B(Base):
            def binop(self, other):
                return B(self.getval() * other.getval())
        def f(x, y, z):
            res = x
            while y > 0:
                myjitdriver.can_enter_jit(y=y, x=x, z=z, res=res)
                myjitdriver.jit_merge_point(y=y, x=x, z=z, res=res)
                res = res.binop(x)
                y -= 1
                if y < 8:
                    x = z
            return res
        def g(x, y):
            c1 = f(A(x), y, B(x))
            c2 = f(A(x), y, B(x))
            assert c1.val == c2.val
            return c1.val
        res = self.meta_interp(g, [3, 16])
        assert res == g(3, 16)

    def test_inlined_guard_in_short_preamble(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'x', 'z', 'res'])
        class A:
            def __init__(self, val):
                self.val = val
            def getval(self):
                return self.val
            def binop(self, other):
                return A(self.getval() + other.getval())
        def f(x, y, z):
            res = x
            while y > 0:
                myjitdriver.can_enter_jit(y=y, x=x, z=z, res=res)
                myjitdriver.jit_merge_point(y=y, x=x, z=z, res=res)
                res = res.binop(x)
                y -= 1
                if y < 7:
                    x = z
            return res
        def g(x, y):
            a1 = f(A(x), y, A(x))
            a2 = f(A(x), y, A(x))
            assert a1.val == a2.val
            return a1.val
        res = self.meta_interp(g, [3, 14])
        assert res == g(3, 14)

    def test_specialied_bridge(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'x', 'res'])
        class A:
            def __init__(self, val):
                self.val = val
            def binop(self, other):
                return A(self.val + other.val)
        def f(x, y):
            res = A(0)
            while y > 0:
                myjitdriver.can_enter_jit(y=y, x=x, res=res)
                myjitdriver.jit_merge_point(y=y, x=x, res=res)
                res = res.binop(A(y))
                if y<7:
                    res = x
                y -= 1
            return res
        def g(x, y):
            a1 = f(A(x), y)
            a2 = f(A(x), y)
            assert a1.val == a2.val
            return a1.val
        res = self.meta_interp(g, [6, 14])
        assert res == g(6, 14)

    def test_specialied_bridge_const(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'const', 'x', 'res'])
        class A:
            def __init__(self, val):
                self.val = val
            def binop(self, other):
                return A(self.val + other.val)
        def f(x, y):
            res = A(0)
            const = 7
            while y > 0:
                myjitdriver.can_enter_jit(y=y, x=x, res=res, const=const)
                myjitdriver.jit_merge_point(y=y, x=x, res=res, const=const)
                const = promote(const)
                res = res.binop(A(const))
                if y<7:
                    res = x
                y -= 1
            return res
        def g(x, y):
            a1 = f(A(x), y)
            a2 = f(A(x), y)
            assert a1.val == a2.val
            return a1.val
        res = self.meta_interp(g, [6, 14])
        assert res == g(6, 14)

    def test_multiple_specialied_zigzag(self):
        myjitdriver = JitDriver(greens = [], reds = ['y', 'x', 'res'])
        class Base:
            def __init__(self, val):
                self.val = val
        class A(Base):
            def binop(self, other):
                return A(self.val + other.val)
            def switch(self):
                return B(self.val)
        class B(Base):
            def binop(self, other):
                return B(self.val * other.val)
            def switch(self):
                return A(self.val)
        def f(x, y):
            res = x
            while y > 0:
                myjitdriver.can_enter_jit(y=y, x=x, res=res)
                myjitdriver.jit_merge_point(y=y, x=x, res=res)
                if y % 4 == 0:
                    res = res.switch()
                res = res.binop(x)
                y -= 1
            return res
        def g(x, y):
            a1 = f(A(x), y)
            a2 = f(A(x), y)
            b1 = f(B(x), y)
            b2 = f(B(x), y)
            assert a1.val == a2.val
            assert b1.val == b2.val
            return a1.val + b1.val
        res = self.meta_interp(g, [3, 23])
        assert res == 7068153
        self.check_loop_count(7)
        self.check_loops(guard_true=4, guard_class=0, int_add=2, int_mul=2,
                         guard_false=2)

    def test_dont_trace_every_iteration(self):
        myjitdriver = JitDriver(greens = [], reds = ['a', 'b', 'i', 'sa'])

        def main(a, b):
            i = sa = 0
            #while i < 200:
            while i < 200:
                myjitdriver.can_enter_jit(a=a, b=b, i=i, sa=sa)
                myjitdriver.jit_merge_point(a=a, b=b, i=i, sa=sa)
                if a > 0: pass
                if b < 2: pass
                sa += a % b
                i += 1
            return sa
        def g():
            return main(10, 20) + main(-10, -20)
        res = self.meta_interp(g, [])
        assert res == g()
        self.check_enter_count(2)

    def test_current_trace_length(self):
        myjitdriver = JitDriver(greens = ['g'], reds = ['x'])
        @dont_look_inside
        def residual():
            print "hi there"
        @unroll_safe
        def loop(g):
            y = 0
            while y < g:
                residual()
                y += 1
        def f(x, g):
            n = 0
            while x > 0:
                myjitdriver.can_enter_jit(x=x, g=g)
                myjitdriver.jit_merge_point(x=x, g=g)
                loop(g)
                x -= 1
                n = current_trace_length()
            return n
        res = self.meta_interp(f, [5, 8])
        assert 14 < res < 42
        res = self.meta_interp(f, [5, 2])
        assert 4 < res < 14

    def test_compute_identity_hash(self):
        from pypy.rlib.objectmodel import compute_identity_hash
        class A(object):
            pass
        def f():
            a = A()
            return compute_identity_hash(a) == compute_identity_hash(a)
        res = self.interp_operations(f, [])
        assert res
        # a "did not crash" kind of test

    def test_compute_unique_id(self):
        from pypy.rlib.objectmodel import compute_unique_id
        class A(object):
            pass
        def f():
            a1 = A()
            a2 = A()
            return (compute_unique_id(a1) == compute_unique_id(a1) and
                    compute_unique_id(a1) != compute_unique_id(a2))
        res = self.interp_operations(f, [])
        assert res

    def test_wrap_around_add(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'n'])
        class A:
            pass
        def f(x):
            n = 0
            while x > 0:
                myjitdriver.can_enter_jit(x=x, n=n)
                myjitdriver.jit_merge_point(x=x, n=n)
                x += 1
                n += 1
            return n
        res = self.meta_interp(f, [sys.maxint-10])
        assert res == 11
        self.check_tree_loop_count(2)

    def test_wrap_around_mul(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'n'])
        class A:
            pass
        def f(x):
            n = 0
            while x > 0:
                myjitdriver.can_enter_jit(x=x, n=n)
                myjitdriver.jit_merge_point(x=x, n=n)
                x *= 2
                n += 1
            return n
        res = self.meta_interp(f, [sys.maxint>>10])
        assert res == 11
        self.check_tree_loop_count(2)

    def test_wrap_around_sub(self):
        myjitdriver = JitDriver(greens = [], reds = ['x', 'n'])
        class A:
            pass
        def f(x):
            n = 0
            while x < 0:
                myjitdriver.can_enter_jit(x=x, n=n)
                myjitdriver.jit_merge_point(x=x, n=n)
                x -= 1
                n += 1
            return n
        res = self.meta_interp(f, [10-sys.maxint])
        assert res == 12
        self.check_tree_loop_count(2)

    def test_caching_setfield(self):
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'i', 'n', 'a', 'node'])
        class A:
            pass
        def f(n, a):
            i = sa = 0
            node = A()
            node.val1 = node.val2 = 0
            while i < n:
                myjitdriver.can_enter_jit(sa=sa, i=i, n=n, a=a, node=node)
                myjitdriver.jit_merge_point(sa=sa, i=i, n=n, a=a, node=node)
                sa += node.val1 + node.val2
                if i < n/2:
                    node.val1 = a
                    node.val2 = a
                else:
                    node.val1 = a
                    node.val2 = a + 1
                i += 1
            return sa
        res = self.meta_interp(f, [32, 7])
        assert res == f(32, 7)

    def test_caching_setarrayitem_fixed(self):
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'i', 'n', 'a', 'node'])
        def f(n, a):
            i = sa = 0
            node = [1, 2, 3]
            while i < n:
                myjitdriver.can_enter_jit(sa=sa, i=i, n=n, a=a, node=node)
                myjitdriver.jit_merge_point(sa=sa, i=i, n=n, a=a, node=node)
                sa += node[0] + node[1]
                if i < n/2:
                    node[0] = a
                    node[1] = a
                else:
                    node[0] = a
                    node[1] = a + 1
                i += 1
            return sa
        res = self.meta_interp(f, [32, 7])
        assert res == f(32, 7)

    def test_caching_setarrayitem_var(self):
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'i', 'n', 'a', 'b', 'node'])
        def f(n, a, b):
            i = sa = 0
            node = [1, 2, 3]
            while i < n:
                myjitdriver.can_enter_jit(sa=sa, i=i, n=n, a=a, b=b, node=node)
                myjitdriver.jit_merge_point(sa=sa, i=i, n=n, a=a, b=b, node=node)
                sa += node[0] + node[b]
                if i < n/2:
                    node[0] = a
                    node[b] = a
                else:
                    node[0] = a
                    node[b] = a + 1
                i += 1
            return sa
        res = self.meta_interp(f, [32, 7, 2])
        assert res == f(32, 7, 2)

    def test_getfield_result_with_intbound(self):
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'i', 'n', 'a', 'node'])
        class A:
            pass
        def f(n, a):
            i = sa = 0
            node = A()
            node.val1 = a
            while i < n:
                myjitdriver.can_enter_jit(sa=sa, i=i, n=n, a=a, node=node)
                myjitdriver.jit_merge_point(sa=sa, i=i, n=n, a=a, node=node)
                if node.val1 > 0:
                    sa += 1
                if i > n/2:
                    node.val1 = -a
                i += 1
            return sa
        res = self.meta_interp(f, [32, 7])
        assert res == f(32, 7)

    def test_getfield_result_constant(self):
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'i', 'n', 'a', 'node'])
        class A:
            pass
        def f(n, a):
            i = sa = 0
            node = A()
            node.val1 = 7
            while i < n:
                myjitdriver.can_enter_jit(sa=sa, i=i, n=n, a=a, node=node)
                myjitdriver.jit_merge_point(sa=sa, i=i, n=n, a=a, node=node)
                if node.val1 == 7:
                    sa += 1
                if i > n/2:
                    node.val1 = -7
                i += 1
            return sa
        res = self.meta_interp(f, [32, 7])
        assert res == f(32, 7)

    def test_overflowing_shift_pos(self):
        myjitdriver = JitDriver(greens = [], reds = ['a', 'b', 'n', 'sa'])
        def f1(a, b):
            n = sa = 0
            while n < 10:
                myjitdriver.jit_merge_point(a=a, b=b, n=n, sa=sa)
                if 0 < a <= 5: pass
                if 0 < b <= 5: pass
                sa += (((((a << b) << b) << b) >> b) >> b) >> b
                n += 1
            return sa

        def f2(a, b):
            n = sa = 0
            while n < 10:
                myjitdriver.jit_merge_point(a=a, b=b, n=n, sa=sa)
                if 0 < a < promote(sys.maxint/2): pass
                if 0 < b < 100: pass
                sa += (((((a << b) << b) << b) >> b) >> b) >> b
                n += 1
            return sa

        assert self.meta_interp(f1, [5, 5]) == 50
        self.check_loops(int_rshift=0, everywhere=True)

        for f in (f1, f2):
            assert self.meta_interp(f, [5, 6]) == 50
            self.check_loops(int_rshift=3, everywhere=True)

            assert self.meta_interp(f, [10, 5]) == 100
            self.check_loops(int_rshift=3, everywhere=True)

            assert self.meta_interp(f, [10, 6]) == 100
            self.check_loops(int_rshift=3, everywhere=True)

            assert self.meta_interp(f, [5, 31]) == 0
            self.check_loops(int_rshift=3, everywhere=True)

            bigval = 1
            while (bigval << 3).__class__ is int:
                bigval = bigval << 1

            assert self.meta_interp(f, [bigval, 5]) == 0
            self.check_loops(int_rshift=3, everywhere=True)

    def test_overflowing_shift_neg(self):
        myjitdriver = JitDriver(greens = [], reds = ['a', 'b', 'n', 'sa'])
        def f1(a, b):
            n = sa = 0
            while n < 10:
                myjitdriver.jit_merge_point(a=a, b=b, n=n, sa=sa)
                if -5 <= a < 0: pass
                if 0 < b <= 5: pass
                sa += (((((a << b) << b) << b) >> b) >> b) >> b
                n += 1
            return sa

        def f2(a, b):
            n = sa = 0
            while n < 10:
                myjitdriver.jit_merge_point(a=a, b=b, n=n, sa=sa)
                if -promote(sys.maxint/2) < a < 0: pass
                if 0 < b < 100: pass
                sa += (((((a << b) << b) << b) >> b) >> b) >> b
                n += 1
            return sa

        assert self.meta_interp(f1, [-5, 5]) == -50
        self.check_loops(int_rshift=0, everywhere=True)

        for f in (f1, f2):
            assert self.meta_interp(f, [-5, 6]) == -50
            self.check_loops(int_rshift=3, everywhere=True)

            assert self.meta_interp(f, [-10, 5]) == -100
            self.check_loops(int_rshift=3, everywhere=True)

            assert self.meta_interp(f, [-10, 6]) == -100
            self.check_loops(int_rshift=3, everywhere=True)

            assert self.meta_interp(f, [-5, 31]) == 0
            self.check_loops(int_rshift=3, everywhere=True)

            bigval = 1
            while (bigval << 3).__class__ is int:
                bigval = bigval << 1

            assert self.meta_interp(f, [bigval, 5]) == 0
            self.check_loops(int_rshift=3, everywhere=True)

    def test_pure_op_not_to_be_propagated(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'sa'])
        def f(n):
            sa = 0
            while n > 0:
                myjitdriver.jit_merge_point(n=n, sa=sa)
                sa += n + 1
                n -= 1
            return sa
        assert self.meta_interp(f, [10]) == f(10)

    def test_inputarg_reset_bug(self):
        ## j = 0
        ## while j < 100:
        ##     j += 1

        ## c = 0
        ## j = 0
        ## while j < 2:
        ##     j += 1
        ##     if c == 0:
        ##         c = 1
        ##     else:
        ##         c = 0

        ## j = 0
        ## while j < 100:
        ##     j += 1

        def get_printable_location(i):
            return str(i)

        myjitdriver = JitDriver(greens = ['i'], reds = ['j', 'c', 'a'],
                                get_printable_location=get_printable_location)
        bytecode = "0j10jc20a3"
        def f():
            myjitdriver.set_param('threshold', 7)
            myjitdriver.set_param('trace_eagerness', 1)
            i = j = c = a = 1
            while True:
                myjitdriver.jit_merge_point(i=i, j=j, c=c, a=a)
                if i >= len(bytecode):
                    break
                op = bytecode[i]
                if op == 'j':
                    j += 1
                elif op == 'c':
                    promote(c)
                    c = 1 - c
                elif op == '2':
                    if j < 3:
                        i -= 3
                        myjitdriver.can_enter_jit(i=i, j=j, c=c, a=a)
                elif op == '1':
                    k = j*a
                    if j < 100:
                        i -= 2
                        a += k
                        myjitdriver.can_enter_jit(i=i, j=j, c=c, a=a)
                    else:
                        a += k*2
                elif op == '0':
                    j = c = a = 0
                elif op == 'a':
                    j += 1
                    a += 1
                elif op == '3':
                    if a < 100:
                        i -= 2
                        myjitdriver.can_enter_jit(i=i, j=j, c=c, a=a)

                else:
                    return ord(op)
                i += 1
            return 42
        assert f() == 42
        def g():
            res = 1
            for i in range(10):
                res = f()
            return res
        res = self.meta_interp(g, [])
        assert res == 42

    def test_read_timestamp(self):
        import time
        from pypy.rlib.rtimer import read_timestamp
        def busy_loop():
            start = time.time()
            while time.time() - start < 0.1:
                # busy wait
                pass

        def f():
            t1 = read_timestamp()
            busy_loop()
            t2 = read_timestamp()
            return t2 - t1 > 1000
        res = self.interp_operations(f, [])
        assert res

    def test_bug688_multiple_immutable_fields(self):
        myjitdriver = JitDriver(greens=[], reds=['counter','context'])

        class Tag:
            pass
        class InnerContext():
            _immutable_fields_ = ['variables','local_names']
            def __init__(self, variables):
                self.variables = variables
                self.local_names = [0]

            def store(self):
                self.local_names[0] = 1

            def retrieve(self):
                variables = self.variables
                promote(variables)
                result = self.local_names[0]
                if result == 0:
                    return -1
                else:
                    return -1
        def build():
            context = InnerContext(Tag())

            context.store()

            counter = 0
            while True:
                myjitdriver.jit_merge_point(context=context, counter = counter)
                context.retrieve()
                context.retrieve()

                counter += 1
                if counter > 10:
                    return 7
        assert self.meta_interp(build, []) == 7
        self.check_loops(getfield_gc_pure=0)
        self.check_loops(getfield_gc_pure=2, everywhere=True)

    def test_args_becomming_equal(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'a', 'b'])
        def f(n, a, b):
            sa = i = 0
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, a=a, b=b)
                sa += a
                sa *= b
                if i > n/2:
                    a = b
                i += 1
            return sa
        assert self.meta_interp(f, [20, 1, 2]) == f(20, 1, 2)

    def test_args_becomming_equal_boxed1(self):
        class A(object):
            def __init__(self, a, b):
                self.a = a
                self.b = b
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'a', 'b', 'node'])

        def f(n, a, b):
            sa = i = 0
            node = A(a,b)
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, a=a, b=b, node=node)
                sa += node.a
                sa *= node.b
                if i > n/2:
                    node = A(b, b)
                else:
                    node = A(a, b)
                i += 1
            return sa
        assert self.meta_interp(f, [20, 1, 2]) == f(20, 1, 2)

    def test_args_becomming_not_equal_boxed1(self):
        class A(object):
            def __init__(self, a, b):
                self.a = a
                self.b = b
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'a', 'b', 'node'])

        def f(n, a, b):
            sa = i = 0
            node = A(b, b)
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, a=a, b=b, node=node)
                sa += node.a
                sa *= node.b
                if i > n/2:
                    node = A(a, b)
                else:
                    node = A(b, b)
                i += 1
            return sa
        assert self.meta_interp(f, [20, 1, 2]) == f(20, 1, 2)

    def test_args_becomming_equal_boxed2(self):
        class A(object):
            def __init__(self, a, b):
                self.a = a
                self.b = b
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'node'])

        def f(n, a, b):
            sa = i = 0
            node = A(a, b)
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, node=node)
                sa += node.a
                sa *= node.b
                if i > n/2:
                    node = A(node.b, node.b)
                else:
                    node = A(node.b, node.a)
                i += 1
            return sa
        assert self.meta_interp(f, [20, 1, 2]) == f(20, 1, 2)

    def test_inlined_short_preamble_guard_needed_in_loop1(self):
        class A(object):
            def __init__(self, a):
                self.a = a
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa',
                                                     'a', 'b'])
        def f(n, a, b):
            sa = i = 0
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, a=a, b=b)
                if a.a < 10:
                    sa += a.a
                b.a = i
                i += 1
            return sa
        def g(n):
            return f(n, A(5), A(10))
        assert self.meta_interp(g, [20]) == g(20)

    def test_ovf_guard_in_short_preamble2(self):
        class A(object):
            def __init__(self, val):
                self.val = val
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'a', 'node1', 'node2'])
        def f(n, a):
            node1 = node2 = A(0)
            sa = i = 0
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, a=a, node1=node1, node2=node2)
                node2.val = 7
                if a >= 100:
                    sa += 1
                sa += ovfcheck(i + i)
                node1 = A(i)
                i += 1
        assert self.meta_interp(f, [20, 7]) == f(20, 7)

    def test_intbounds_generalized(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa'])

        def f(n):
            sa = i = 0
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa)
                if i > n/2:
                    sa += 1
                else:
                    sa += 2
                i += 1
            return sa
        assert self.meta_interp(f, [20]) == f(20)
        self.check_loops(int_gt=1, int_lt=2, int_ge=0, int_le=0)

    def test_intbounds_not_generalized1(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa'])

        def f(n):
            sa = i = 0
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa)
                if i > n/2:
                    sa += 1
                else:
                    sa += 2
                    assert  -100 < i < 100
                i += 1
            return sa
        assert self.meta_interp(f, [20]) == f(20)
        self.check_loops(int_gt=1, int_lt=3, int_ge=2, int_le=1)

    def test_intbounds_not_generalized2(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'node'])
        class A(object):
            def __init__(self, val):
                self.val = val
        def f(n):
            sa = i = 0
            node = A(n)
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, node=node)
                if i > n/2:
                    sa += 1
                else:
                    sa += 2
                    assert  -100 <= node.val <= 100
                i += 1
            return sa
        assert self.meta_interp(f, [20]) == f(20)
        self.check_loops(int_gt=1, int_lt=2, int_ge=1, int_le=1)

    def test_retrace_limit1(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'a'])

        def f(n, limit):
            myjitdriver.set_param('retrace_limit', limit)
            sa = i = a = 0
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, a=a)
                a = i/4
                a = hint(a, promote=True)
                sa += a
                i += 1
            return sa
        assert self.meta_interp(f, [20, 2]) == f(20, 2)
        self.check_tree_loop_count(4)
        assert self.meta_interp(f, [20, 3]) == f(20, 3)
        self.check_tree_loop_count(5)

    def test_max_retrace_guards(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'a'])

        def f(n, limit):
            myjitdriver.set_param('retrace_limit', 3)
            myjitdriver.set_param('max_retrace_guards', limit)
            sa = i = a = 0
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, a=a)
                a = i/4
                a = hint(a, promote=True)
                sa += a
                i += 1
            return sa
        assert self.meta_interp(f, [20, 1]) == f(20, 1)
        self.check_tree_loop_count(2)
        assert self.meta_interp(f, [20, 10]) == f(20, 10)
        self.check_tree_loop_count(5)


    def test_retrace_limit_with_extra_guards(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'i', 'sa', 'a',
                                                     'node'])
        def f(n, limit):
            myjitdriver.set_param('retrace_limit', limit)
            sa = i = a = 0
            node = [1, 2, 3]
            node[1] = n
            while i < n:
                myjitdriver.jit_merge_point(n=n, i=i, sa=sa, a=a, node=node)
                a = i/4
                a = hint(a, promote=True)
                if i&1 == 0:
                    sa += node[i%3]
                sa += a
                i += 1
            return sa
        assert self.meta_interp(f, [20, 2]) == f(20, 2)
        self.check_tree_loop_count(4)
        assert self.meta_interp(f, [20, 3]) == f(20, 3)
        self.check_tree_loop_count(5)

    def test_retrace_ending_up_retrazing_another_loop(self):

        myjitdriver = JitDriver(greens = ['pc'], reds = ['n', 'i', 'sa'])
        bytecode = "0+sI0+SI"
        def f(n):
            myjitdriver.set_param('threshold', 3)
            myjitdriver.set_param('trace_eagerness', 1)
            myjitdriver.set_param('retrace_limit', 5)
            myjitdriver.set_param('function_threshold', -1)
            pc = sa = i = 0
            while pc < len(bytecode):
                myjitdriver.jit_merge_point(pc=pc, n=n, sa=sa, i=i)
                n = hint(n, promote=True)
                op = bytecode[pc]
                if op == '0':
                    i = 0
                elif op == '+':
                    i += 1
                elif op == 's':
                    sa += i
                elif op == 'S':
                    sa += 2
                elif op == 'I':
                    if i < n:
                        pc -= 2
                        myjitdriver.can_enter_jit(pc=pc, n=n, sa=sa, i=i)
                        continue
                pc += 1
            return sa

        def g(n1, n2):
            for i in range(10):
                f(n1)
            for i in range(10):
                f(n2)

        nn = [10, 3]
        assert self.meta_interp(g, nn) == g(*nn)

        # The attempts of retracing first loop will end up retracing the
        # second and thus fail 5 times, saturating the retrace_count. Instead a
        # bridge back to the preamble of the first loop is produced. A guard in
        # this bridge is later traced resulting in a retrace of the second loop.
        # Thus we end up with:
        #   1 preamble and 1 specialized version of first loop
        #   1 preamble and 2 specialized version of second loop
        self.check_tree_loop_count(2 + 3)

        # FIXME: Add a gloabl retrace counter and test that we are not trying more than 5 times.

        def g(n):
            for i in range(n):
                for j in range(10):
                    f(n-i)

        res = self.meta_interp(g, [10])
        assert res == g(10)
        # 1 preamble and 6 speciealized versions of each loop
        self.check_tree_loop_count(2*(1 + 6))

    def test_nested_retrace(self):

        myjitdriver = JitDriver(greens = ['pc'], reds = ['n', 'a', 'i', 'j', 'sa'])
        bytecode = "ij+Jj+JI"
        def f(n, a):
            myjitdriver.set_param('threshold', 5)
            myjitdriver.set_param('trace_eagerness', 1)
            myjitdriver.set_param('retrace_limit', 2)
            pc = sa = i = j = 0
            while pc < len(bytecode):
                myjitdriver.jit_merge_point(pc=pc, n=n, sa=sa, i=i, j=j, a=a)
                a = hint(a, promote=True)
                op = bytecode[pc]
                if op == 'i':
                    i = 0
                elif op == 'j':
                    j = 0
                elif op == '+':
                    sa += a
                elif op == 'J':
                    j += 1
                    if j < 3:
                        pc -= 1
                        myjitdriver.can_enter_jit(pc=pc, n=n, sa=sa, i=i, j=j, a=a)
                        continue
                elif op == 'I':
                    i += 1
                    if i < n:
                        pc -= 6
                        myjitdriver.can_enter_jit(pc=pc, n=n, sa=sa, i=i, j=j, a=a)
                        continue
                pc += 1
            return sa

        res = self.meta_interp(f, [10, 7])
        assert res == f(10, 7)
        self.check_tree_loop_count(4)

        def g(n):
            return f(n, 2) + f(n, 3)

        res = self.meta_interp(g, [10])
        assert res == g(10)
        self.check_tree_loop_count(6)


        def g(n):
            return f(n, 2) + f(n, 3) + f(n, 4) + f(n, 5) + f(n, 6) + f(n, 7)

        res = self.meta_interp(g, [10])
        assert res == g(10)
        self.check_tree_loop_count(8)

    def test_frame_finished_during_retrace(self):
        class Base(object):
            pass
        class A(Base):
            def __init__(self, a):
                self.val = a
                self.num = 1
            def inc(self):
                return A(self.val + 1)
        class B(Base):
            def __init__(self, a):
                self.val = a
                self.num = 1000
            def inc(self):
                return B(self.val + 1)
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'a'])
        def f():
            myjitdriver.set_param('threshold', 3)
            myjitdriver.set_param('trace_eagerness', 2)
            a = A(0)
            sa = 0
            while a.val < 8:
                myjitdriver.jit_merge_point(a=a, sa=sa)
                a = a.inc()
                if a.val > 4:
                    a = B(a.val)
                sa += a.num
            return sa
        res = self.meta_interp(f, [])
        assert res == f()

    def test_frame_finished_during_continued_retrace(self):
        class Base(object):
            pass
        class A(Base):
            def __init__(self, a):
                self.val = a
                self.num = 100
            def inc(self):
                return A(self.val + 1)
        class B(Base):
            def __init__(self, a):
                self.val = a
                self.num = 10000
            def inc(self):
                return B(self.val + 1)
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'b', 'a'])
        def f(b):
            myjitdriver.set_param('threshold', 6)
            myjitdriver.set_param('trace_eagerness', 4)
            a = A(0)
            sa = 0
            while a.val < 15:
                myjitdriver.jit_merge_point(a=a, b=b, sa=sa)
                a = a.inc()
                if a.val > 8:
                    a = B(a.val)
                if b == 1:
                    b = 2
                else:
                    b = 1
                sa += a.num + b
            return sa
        res = self.meta_interp(f, [1])
        assert res == f(1)

    def test_remove_array_operations(self):
        myjitdriver = JitDriver(greens = [], reds = ['a'])
        class W_Int:
            def __init__(self, intvalue):
                self.intvalue = intvalue
        def f(x):
            a = [W_Int(x)]
            while a[0].intvalue > 0:
                myjitdriver.jit_merge_point(a=a)
                a[0] = W_Int(a[0].intvalue - 3)
            return a[0].intvalue
        res = self.meta_interp(f, [100])
        assert res == -2
        #self.check_loops(getarrayitem_gc=0, setarrayitem_gc=0) -- xxx?

    def test_retrace_ending_up_retracing_another_loop(self):

        myjitdriver = JitDriver(greens = ['pc'], reds = ['n', 'i', 'sa'])
        bytecode = "0+sI0+SI"
        def f(n):
            myjitdriver.set_param('threshold', 3)
            myjitdriver.set_param('trace_eagerness', 1)
            myjitdriver.set_param('retrace_limit', 5)
            myjitdriver.set_param('function_threshold', -1)
            pc = sa = i = 0
            while pc < len(bytecode):
                myjitdriver.jit_merge_point(pc=pc, n=n, sa=sa, i=i)
                n = hint(n, promote=True)
                op = bytecode[pc]
                if op == '0':
                    i = 0
                elif op == '+':
                    i += 1
                elif op == 's':
                    sa += i
                elif op == 'S':
                    sa += 2
                elif op == 'I':
                    if i < n:
                        pc -= 2
                        myjitdriver.can_enter_jit(pc=pc, n=n, sa=sa, i=i)
                        continue
                pc += 1
            return sa

        def g(n1, n2):
            for i in range(10):
                f(n1)
            for i in range(10):
                f(n2)

        nn = [10, 3]
        assert self.meta_interp(g, nn) == g(*nn)

        # The attempts of retracing first loop will end up retracing the
        # second and thus fail 5 times, saturating the retrace_count. Instead a
        # bridge back to the preamble of the first loop is produced. A guard in
        # this bridge is later traced resulting in a retrace of the second loop.
        # Thus we end up with:
        #   1 preamble and 1 specialized version of first loop
        #   1 preamble and 2 specialized version of second loop
        self.check_tree_loop_count(2 + 3)

        # FIXME: Add a gloabl retrace counter and test that we are not trying more than 5 times.

        def g(n):
            for i in range(n):
                for j in range(10):
                    f(n-i)

        res = self.meta_interp(g, [10])
        assert res == g(10)
        # 1 preamble and 6 speciealized versions of each loop
        self.check_tree_loop_count(2*(1 + 6))

    def test_continue_tracing_with_boxes_in_start_snapshot_replaced_by_optimizer(self):
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'n', 'a', 'b'])
        def f(n):
            sa = a = 0
            b = 10
            while n:
                myjitdriver.jit_merge_point(sa=sa, n=n, a=a, b=b)
                sa += b
                b += 1
                if b > 7:
                    pass
                if a == 0:
                    a = 1
                elif a == 1:
                    a = 2
                elif a == 2:
                    a = 0
                sa += a
                sa += 0
                n -= 1
            return sa
        res = self.meta_interp(f, [16])
        assert res == f(16)

    def test_loopinvariant_array_shrinking1(self):
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'n', 'i', 'a'])
        def f(n):
            sa = i = 0
            a = [0, 1, 2, 3, 4]
            while i < n:
                myjitdriver.jit_merge_point(sa=sa, n=n, a=a, i=i)
                if i < n / 2:
                    sa += a[4]
                elif i == n / 2:
                    a.pop()
                i += 1
        res = self.meta_interp(f, [32])
        assert res == f(32)
        self.check_loops(arraylen_gc=2)

    def test_ulonglong_mod(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'sa', 'i'])
        def f(n):
            sa = i = rffi.cast(rffi.ULONGLONG, 1)
            while i < rffi.cast(rffi.ULONGLONG, n):
                myjitdriver.jit_merge_point(sa=sa, n=n, i=i)
                sa += sa % i
                i += 1
        res = self.meta_interp(f, [32])
        assert res == f(32)


class TestOOtype(BasicTests, OOJitMixin):

    def test_oohash(self):
        def f(n):
            s = ootype.oostring(n, -1)
            return s.ll_hash()
        res = self.interp_operations(f, [5])
        assert res == ootype.oostring(5, -1).ll_hash()

    def test_identityhash(self):
        A = ootype.Instance("A", ootype.ROOT)
        def f():
            obj1 = ootype.new(A)
            obj2 = ootype.new(A)
            return ootype.identityhash(obj1) == ootype.identityhash(obj2)
        assert not f()
        res = self.interp_operations(f, [])
        assert not res

    def test_oois(self):
        A = ootype.Instance("A", ootype.ROOT)
        def f(n):
            obj1 = ootype.new(A)
            if n:
                obj2 = obj1
            else:
                obj2 = ootype.new(A)
            return obj1 is obj2
        res = self.interp_operations(f, [0])
        assert not res
        res = self.interp_operations(f, [1])
        assert res

    def test_oostring_instance(self):
        A = ootype.Instance("A", ootype.ROOT)
        B = ootype.Instance("B", ootype.ROOT)
        def f(n):
            obj1 = ootype.new(A)
            obj2 = ootype.new(B)
            s1 = ootype.oostring(obj1, -1)
            s2 = ootype.oostring(obj2, -1)
            ch1 = s1.ll_stritem_nonneg(1)
            ch2 = s2.ll_stritem_nonneg(1)
            return ord(ch1) + ord(ch2)
        res = self.interp_operations(f, [0])
        assert res == ord('A') + ord('B')

    def test_subclassof(self):
        A = ootype.Instance("A", ootype.ROOT)
        B = ootype.Instance("B", A)
        clsA = ootype.runtimeClass(A)
        clsB = ootype.runtimeClass(B)
        myjitdriver = JitDriver(greens = [], reds = ['n', 'flag', 'res'])

        def getcls(flag):
            if flag:
                return clsA
            else:
                return clsB

        def f(flag, n):
            res = True
            while n > -100:
                myjitdriver.can_enter_jit(n=n, flag=flag, res=res)
                myjitdriver.jit_merge_point(n=n, flag=flag, res=res)
                cls = getcls(flag)
                n -= 1
                res = ootype.subclassof(cls, clsB)
            return res

        res = self.meta_interp(f, [1, 100],
                               policy=StopAtXPolicy(getcls),
                               enable_opts='')
        assert not res

        res = self.meta_interp(f, [0, 100],
                               policy=StopAtXPolicy(getcls),
                               enable_opts='')
        assert res

class BaseLLtypeTests(BasicTests):

    def test_identityhash(self):
        A = lltype.GcStruct("A")
        def f():
            obj1 = lltype.malloc(A)
            obj2 = lltype.malloc(A)
            return lltype.identityhash(obj1) == lltype.identityhash(obj2)
        assert not f()
        res = self.interp_operations(f, [])
        assert not res

    def test_oops_on_nongc(self):
        from pypy.rpython.lltypesystem import lltype

        TP = lltype.Struct('x')
        def f(i1, i2):
            p1 = prebuilt[i1]
            p2 = prebuilt[i2]
            a = p1 is p2
            b = p1 is not p2
            c = bool(p1)
            d = not bool(p2)
            return 1000*a + 100*b + 10*c + d
        prebuilt = [lltype.malloc(TP, flavor='raw', immortal=True)] * 2
        expected = f(0, 1)
        assert self.interp_operations(f, [0, 1]) == expected

    def test_casts(self):
        py.test.skip("xxx fix or kill")
        if not self.basic:
            py.test.skip("test written in a style that "
                         "means it's frontend only")
        from pypy.rpython.lltypesystem import lltype, llmemory, rffi

        TP = lltype.GcStruct('S1')
        def f(p):
            n = lltype.cast_ptr_to_int(p)
            return n
        x = lltype.malloc(TP)
        xref = lltype.cast_opaque_ptr(llmemory.GCREF, x)
        res = self.interp_operations(f, [xref])
        y = llmemory.cast_ptr_to_adr(x)
        y = llmemory.cast_adr_to_int(y)
        assert rffi.get_real_int(res) == rffi.get_real_int(y)
        #
        TP = lltype.Struct('S2')
        prebuilt = [lltype.malloc(TP, immortal=True),
                    lltype.malloc(TP, immortal=True)]
        def f(x):
            p = prebuilt[x]
            n = lltype.cast_ptr_to_int(p)
            return n
        res = self.interp_operations(f, [1])
        y = llmemory.cast_ptr_to_adr(prebuilt[1])
        y = llmemory.cast_adr_to_int(y)
        assert rffi.get_real_int(res) == rffi.get_real_int(y)

    def test_collapsing_ptr_eq(self):
        S = lltype.GcStruct('S')
        p = lltype.malloc(S)
        driver = JitDriver(greens = [], reds = ['n', 'x'])

        def f(n, x):
            while n > 0:
                driver.can_enter_jit(n=n, x=x)
                driver.jit_merge_point(n=n, x=x)
                if x:
                    n -= 1
                n -= 1

        def main():
            f(10, p)
            f(10, lltype.nullptr(S))

        self.meta_interp(main, [])

    def test_enable_opts(self):
        jitdriver = JitDriver(greens = [], reds = ['a'])

        class A(object):
            def __init__(self, i):
                self.i = i

        def f():
            a = A(0)

            while a.i < 10:
                jitdriver.jit_merge_point(a=a)
                jitdriver.can_enter_jit(a=a)
                a = A(a.i + 1)

        self.meta_interp(f, [])
        self.check_loops(new_with_vtable=0)
        self.meta_interp(f, [], enable_opts='')
        self.check_loops(new_with_vtable=1)

    def test_two_loopinvariant_arrays1(self):
        from pypy.rpython.lltypesystem import lltype, llmemory, rffi
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'n', 'i', 'a'])
        TP = lltype.GcArray(lltype.Signed)
        def f(n):
            sa = i = 0
            a = lltype.malloc(TP, 5)
            a[4] = 7
            while i < n:
                myjitdriver.jit_merge_point(sa=sa, n=n, a=a, i=i)
                if i < n/2:
                    sa += a[4]
                if i == n/2:
                    a = lltype.malloc(TP, 3)
                i += 1
            return sa
        res = self.meta_interp(f, [32])
        assert res == f(32)
        self.check_tree_loop_count(3)

    def test_two_loopinvariant_arrays2(self):
        from pypy.rpython.lltypesystem import lltype, llmemory, rffi
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'n', 'i', 'a'])
        TP = lltype.GcArray(lltype.Signed)
        def f(n):
            sa = i = 0
            a = lltype.malloc(TP, 5)
            a[4] = 7
            while i < n:
                myjitdriver.jit_merge_point(sa=sa, n=n, a=a, i=i)
                if i < n/2:
                    sa += a[4]
                elif i > n/2:
                    sa += a[2]
                if i == n/2:
                    a = lltype.malloc(TP, 3)
                    a[2] = 42
                i += 1
            return sa
        res = self.meta_interp(f, [32])
        assert res == f(32)
        self.check_tree_loop_count(3)

    def test_two_loopinvariant_arrays3(self):
        from pypy.rpython.lltypesystem import lltype, llmemory, rffi
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'n', 'i', 'a'])
        TP = lltype.GcArray(lltype.Signed)
        def f(n):
            sa = i = 0
            a = lltype.malloc(TP, 5)
            a[2] = 7
            while i < n:
                myjitdriver.jit_merge_point(sa=sa, n=n, a=a, i=i)
                if i < n/2:
                    sa += a[2]
                elif i > n/2:
                    sa += a[3]
                if i == n/2:
                    a = lltype.malloc(TP, 7)
                    a[3] = 10
                    a[2] = 42
                i += 1
            return sa
        res = self.meta_interp(f, [32])
        assert res == f(32)
        self.check_tree_loop_count(2)

    def test_two_loopinvariant_arrays_boxed(self):
        class A(object):
            def __init__(self, a):
                self.a  = a
        from pypy.rpython.lltypesystem import lltype, llmemory, rffi
        myjitdriver = JitDriver(greens = [], reds = ['sa', 'n', 'i', 'a'])
        TP = lltype.GcArray(lltype.Signed)
        a1 = A(lltype.malloc(TP, 5))
        a2 = A(lltype.malloc(TP, 3))
        def f(n):
            sa = i = 0
            a = a1
            a.a[4] = 7
            while i < n:
                myjitdriver.jit_merge_point(sa=sa, n=n, a=a, i=i)
                if i < n/2:
                    sa += a.a[4]
                if i == n/2:
                    a = a2
                i += 1
            return sa
        res = self.meta_interp(f, [32])
        assert res == f(32)
        self.check_loops(arraylen_gc=2, everywhere=True)

    def test_release_gil_flush_heap_cache(self):
        if sys.platform == "win32":
            py.test.skip("needs 'time'")
        T = rffi.CArrayPtr(rffi.TIME_T)

        external = rffi.llexternal("time", [T], rffi.TIME_T, threadsafe=True)
        # Not a real lock, has all the same properties with respect to GIL
        # release though, so good for this test.
        class Lock(object):
            @dont_look_inside
            def acquire(self):
                external(lltype.nullptr(T.TO))
            @dont_look_inside
            def release(self):
                external(lltype.nullptr(T.TO))
        class X(object):
            def __init__(self, idx):
                self.field = idx
        @dont_look_inside
        def get_obj(z):
            return X(z)
        myjitdriver = JitDriver(greens=[], reds=["n", "l", "z", "lock"])
        def f(n, z):
            lock = Lock()
            l = 0
            while n > 0:
                myjitdriver.jit_merge_point(lock=lock, l=l, n=n, z=z)
                x = get_obj(z)
                l += x.field
                lock.acquire()
                # This must not reuse the previous one.
                n -= x.field
                lock.release()
            return n
        res = self.meta_interp(f, [10, 1])
        self.check_loops(getfield_gc=2)
        assert res == f(10, 1)

    def test_jit_merge_point_with_raw_pointer(self):
        driver = JitDriver(greens = [], reds = ['n', 'x'])

        TP = lltype.Array(lltype.Signed, hints={'nolength': True})

        def f(n):
            x = lltype.malloc(TP, 10, flavor='raw')
            x[0] = 1
            while n > 0:
                driver.jit_merge_point(n=n, x=x)
                n -= x[0]
            lltype.free(x, flavor='raw')
            return n

        self.meta_interp(f, [10], repeat=3)

    def test_jit_merge_point_with_pbc(self):
        driver = JitDriver(greens = [], reds = ['x'])

        class A(object):
            def __init__(self, x):
                self.x = x
            def _freeze_(self):
                return True
        pbc = A(1)

        def main(x):
            return f(x, pbc)

        def f(x, pbc):
            while x > 0:
                driver.jit_merge_point(x = x)
                x -= pbc.x
            return x

        self.meta_interp(main, [10])

    def test_look_inside_iff_const(self):
        @look_inside_iff(lambda arg: isconstant(arg))
        def f(arg):
            s = 0
            while arg > 0:
                s += arg
                arg -= 1
            return s

        driver = JitDriver(greens = ['code'], reds = ['n', 'arg', 's'])

        def main(code, n, arg):
            s = 0
            while n > 0:
                driver.jit_merge_point(code=code, n=n, arg=arg, s=s)
                if code == 0:
                    s += f(arg)
                else:
                    s += f(1)
                n -= 1
            return s

        res = self.meta_interp(main, [0, 10, 2], enable_opts='')
        assert res == main(0, 10, 2)
        self.check_loops(call=1)
        res = self.meta_interp(main, [1, 10, 2], enable_opts='')
        assert res == main(1, 10, 2)
        self.check_loops(call=0)

    def test_look_inside_iff_virtual(self):
        # There's no good reason for this to be look_inside_iff, but it's a test!
        @look_inside_iff(lambda arg, n: isvirtual(arg))
        def f(arg, n):
            if n == 100:
                for i in xrange(n):
                    n += i
            return arg.x
        class A(object):
            def __init__(self, x):
                self.x = x
        driver = JitDriver(greens=['n'], reds=['i', 'a'])
        def main(n):
            i = 0
            a = A(3)
            while i < 20:
                driver.jit_merge_point(i=i, n=n, a=a)
                if n == 0:
                    i += f(a, n)
                else:
                    i += f(A(2), n)
        res = self.meta_interp(main, [0], enable_opts='')
        assert res == main(0)
        self.check_loops(call=1, getfield_gc=0)
        res = self.meta_interp(main, [1], enable_opts='')
        assert res == main(1)
        self.check_loops(call=0, getfield_gc=0)

    def test_reuse_elidable_result(self):
        driver = JitDriver(reds=['n', 's'], greens = [])
        def main(n):
            s = 0
            while n > 0:
                driver.jit_merge_point(s=s, n=n)
                s += len(str(n)) + len(str(n))
                n -= 1
            return s
        res = self.meta_interp(main, [10])
        assert res == main(10)
        self.check_loops({
            'call': 1, 'guard_no_exception': 1, 'guard_true': 1, 'int_add': 2,
            'int_gt': 1, 'int_sub': 1, 'strlen': 1, 'jump': 1,
        })

    def test_look_inside_iff_const_getarrayitem_gc_pure(self):
        driver = JitDriver(greens=['unroll'], reds=['s', 'n'])

        class A(object):
            _immutable_fields_ = ["x[*]"]
            def __init__(self, x):
                self.x = [x]

        @look_inside_iff(lambda x: isconstant(x))
        def f(x):
            i = 0
            for c in x:
                i += 1
            return i

        def main(unroll, n):
            s = 0
            while n > 0:
                driver.jit_merge_point(s=s, n=n, unroll=unroll)
                if unroll:
                    x = A("xx")
                else:
                    x = A("x" * n)
                s += f(x.x[0])
                n -= 1
            return s

        res = self.meta_interp(main, [0, 10])
        assert res == main(0, 10)
        # 2 calls, one for f() and one for char_mul
        self.check_loops(call=2)
        res = self.meta_interp(main, [1, 10])
        assert res == main(1, 10)
        self.check_loops(call=0)

    def test_setarrayitem_followed_by_arraycopy(self):
        myjitdriver = JitDriver(greens = [], reds = ['n', 'sa', 'x', 'y'])
        def f(n):
            sa = 0
            x = [1,2,n]
            y = [1,2,3]
            while n > 0:
                myjitdriver.jit_merge_point(sa=sa, n=n, x=x, y=y)
                y[0] = n
                x[0:3] = y
                sa += x[0]
                n -= 1
            return sa
        res = self.meta_interp(f, [16])
        assert res == f(16)

    def test_ptr_eq_str_constants(self):
        myjitdriver = JitDriver(greens = [], reds = ["n", "x"])
        class A(object):
            def __init__(self, v):
                self.v = v
        def f(n, x):
            while n > 0:
                myjitdriver.jit_merge_point(n=n, x=x)
                z = 0 / x
                a1 = A("key")
                a2 = A("\x00")
                n -= [a1, a2][z].v is not a2.v
            return n
        res = self.meta_interp(f, [10, 1])
        assert res == 0

    def test_virtual_array_of_structs(self):
        myjitdriver = JitDriver(greens = [], reds=["n", "d"])
        def f(n):
            d = None
            while n > 0:
                myjitdriver.jit_merge_point(n=n, d=d)
                d = {}
                if n % 2:
                    d["k"] = n
                else:
                    d["z"] = n
                n -= len(d)
            return n
        res = self.meta_interp(f, [10])
        assert res == 0



class TestLLtype(BaseLLtypeTests, LLJitMixin):
    def test_tagged(self):
        from pypy.rlib.objectmodel import UnboxedValue
        class Base(object):
            __slots__ = ()

        class Int(UnboxedValue, Base):
            __slots__ = ["a"]

            def is_pos(self):
                return self.a > 0

            def dec(self):
                return Int(self.a - 1)


        class Float(Base):
            def __init__(self, a):
                self.a = a

            def is_pos(self):
                return self.a > 0

            def dec(self):
                return Float(self.a - 1)

        driver = JitDriver(greens=['pc', 's'], reds=['o'])

        def main(fl, n, s):
            if s:
                s = "--j"
            else:
                s = "---j"
            if fl:
                o = Float(float(n))
            else:
                o = Int(n)
            pc = 0
            while True:
                driver.jit_merge_point(s=s, pc=pc, o=o)
                c = s[pc]
                if c == "j":
                    driver.can_enter_jit(s=s, pc=pc, o=o)
                    if o.is_pos():
                        pc = 0
                        continue
                    else:
                        break
                elif c == "-":
                    o = o.dec()
                pc += 1
            return pc
        res = self.meta_interp(main, [False, 100, True], taggedpointers=True)

    def test_rerased(self):
        from pypy.rlib.rerased import erase_int, unerase_int, new_erasing_pair
        eraseX, uneraseX = new_erasing_pair("X")
        #
        class X:
            def __init__(self, a, b):
                self.a = a
                self.b = b
        #
        def f(i, j):
            # 'j' should be 0 or 1, not other values
            if j > 0:
                e = eraseX(X(i, j))
            else:
                try:
                    e = erase_int(i)
                except OverflowError:
                    return -42
            if j & 1:
                x = uneraseX(e)
                return x.a - x.b
            else:
                return unerase_int(e)
        #
        x = self.interp_operations(f, [-128, 0], taggedpointers=True)
        assert x == -128
        bigint = sys.maxint//2 + 1
        x = self.interp_operations(f, [bigint, 0], taggedpointers=True)
        assert x == -42
        x = self.interp_operations(f, [1000, 1], taggedpointers=True)
        assert x == 999
