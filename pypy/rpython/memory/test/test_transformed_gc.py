import py
import sys
import inspect
from pypy.translator.c import gc
from pypy.annotation import model as annmodel
from pypy.annotation import policy as annpolicy
from pypy.rpython.lltypesystem import lltype, llmemory, llarena, rffi, llgroup
from pypy.rpython.memory.gctransform import framework
from pypy.rpython.lltypesystem.lloperation import llop, void
from pypy.rlib.objectmodel import compute_unique_id, we_are_translated
from pypy.rlib.debug import ll_assert
from pypy.rlib import rgc
from pypy import conftest
from pypy.rlib.rstring import StringBuilder
from pypy.rlib.rarithmetic import LONG_BIT

WORD = LONG_BIT // 8


def rtype(func, inputtypes, specialize=True, gcname='ref',
          backendopt=False, **extraconfigopts):
    from pypy.translator.translator import TranslationContext
    t = TranslationContext()
    # XXX XXX XXX mess
    t.config.translation.gc = gcname
    t.config.translation.gcremovetypeptr = True
    t.config.set(**extraconfigopts)
    ann = t.buildannotator(policy=annpolicy.StrictAnnotatorPolicy())
    ann.build_types(func, inputtypes)
                                   
    if specialize:
        t.buildrtyper().specialize()
    if backendopt:
        from pypy.translator.backendopt.all import backend_optimizations
        backend_optimizations(t)
    if conftest.option.view:
        t.viewcg()
    return t

ARGS = lltype.FixedSizeArray(lltype.Signed, 3)

class GCTest(object):
    gcpolicy = None
    GC_CAN_MOVE = False
    GC_CAN_MALLOC_NONMOVABLE = True
    taggedpointers = False
    
    def setup_class(cls):
        funcs0 = []
        funcs2 = []
        cleanups = []
        name_to_func = {}
        mixlevelstuff = []
        for fullname in dir(cls):
            if not fullname.startswith('define'):
                continue
            definefunc = getattr(cls, fullname)
            _, name = fullname.split('_', 1)
            func_fixup = definefunc.im_func(cls)
            cleanup = None
            if isinstance(func_fixup, tuple):
                func, cleanup, fixup = func_fixup
                mixlevelstuff.append(fixup)
            else:
                func = func_fixup
            func.func_name = "f_%s" % name
            if cleanup:
                cleanup.func_name = "clean_%s" % name

            nargs = len(inspect.getargspec(func)[0])
            name_to_func[name] = len(funcs0)
            if nargs == 2:
                funcs2.append(func)
                funcs0.append(None)
            elif nargs == 0:
                funcs0.append(func)
                funcs2.append(None)
            else:
                raise NotImplementedError(
                         "defined test functions should have 0/2 arguments")
            # used to let test cleanup static root pointing to runtime
            # allocated stuff
            cleanups.append(cleanup)

        def entrypoint(args):
            num = args[0]
            func = funcs0[num]
            if func:
                res = func()
            else:
                func = funcs2[num]
                res = func(args[1], args[2])
            cleanup = cleanups[num]
            if cleanup:
                cleanup()
            return res

        from pypy.translator.c.genc import CStandaloneBuilder

        s_args = annmodel.SomePtr(lltype.Ptr(ARGS))
        t = rtype(entrypoint, [s_args], gcname=cls.gcname,
                  taggedpointers=cls.taggedpointers)

        for fixup in mixlevelstuff:
            if fixup:
                fixup(t)

        cbuild = CStandaloneBuilder(t, entrypoint, config=t.config,
                                    gcpolicy=cls.gcpolicy)
        db = cbuild.generate_graphs_for_llinterp()
        entrypointptr = cbuild.getentrypointptr()
        entrygraph = entrypointptr._obj.graph
        if conftest.option.view:
            t.viewcg()

        cls.name_to_func = name_to_func
        cls.entrygraph = entrygraph
        cls.rtyper = t.rtyper
        cls.db = db

    def runner(self, name, statistics=False, transformer=False):
        db = self.db
        name_to_func = self.name_to_func
        entrygraph = self.entrygraph
        from pypy.rpython.llinterp import LLInterpreter

        llinterp = LLInterpreter(self.rtyper)

        gct = db.gctransformer

        if self.__class__.__dict__.get('_used', False):
            teardowngraph = gct.frameworkgc__teardown_ptr.value._obj.graph
            llinterp.eval_graph(teardowngraph, [])
        self.__class__._used = True

        # FIIIIISH
        setupgraph = gct.frameworkgc_setup_ptr.value._obj.graph
        # setup => resets the gc
        llinterp.eval_graph(setupgraph, [])
        def run(args):
            ll_args = lltype.malloc(ARGS, immortal=True)
            ll_args[0] = name_to_func[name]
            for i in range(len(args)):
                ll_args[1+i] = args[i]
            res = llinterp.eval_graph(entrygraph, [ll_args])
            return res

        if statistics:
            statisticsgraph = gct.statistics_ptr.value._obj.graph
            ll_gc = gct.c_const_gc.value
            def statistics(index):
                return llinterp.eval_graph(statisticsgraph, [ll_gc, index])
            return run, statistics
        elif transformer:
            return run, gct
        else:
            return run
        
class GenericGCTests(GCTest):
    GC_CAN_SHRINK_ARRAY = False

    def heap_usage(self, statistics):
        try:
            GCClass = self.gcpolicy.transformerclass.GCClass
        except AttributeError:
            from pypy.rpython.memory.gc.marksweep import MarkSweepGC as GCClass
        if hasattr(GCClass, 'STAT_HEAP_USAGE'):
            return statistics(GCClass.STAT_HEAP_USAGE)
        else:
            return -1     # xxx

    def define_instances(cls):
        class A(object):
            pass
        class B(A):
            def __init__(self, something):
                self.something = something
        def malloc_a_lot():
            i = 0
            first = None
            while i < 10:
                i += 1
                a = somea = A()
                a.last = first
                first = a
                j = 0
                while j < 30:
                    b = B(somea)
                    b.last = first
                    j += 1
            return 0
        return malloc_a_lot
    
    def test_instances(self):
        run, statistics = self.runner("instances", statistics=True)
        run([])
        heap_size = self.heap_usage(statistics)


    def define_llinterp_lists(cls):
        def malloc_a_lot():
            i = 0
            while i < 10:
                i += 1
                a = [1] * 10
                j = 0
                while j < 30:
                    j += 1
                    a.append(j)
            return 0
        return malloc_a_lot

    def test_llinterp_lists(self):
        run, statistics = self.runner("llinterp_lists", statistics=True)
        run([])
        heap_size = self.heap_usage(statistics)
        assert heap_size < 16000 * WORD / 4 # xxx

    def define_llinterp_tuples(cls):
        def malloc_a_lot():
            i = 0
            while i < 10:
                i += 1
                a = (1, 2, i)
                b = [a] * 10
                j = 0
                while j < 20:
                    j += 1
                    b.append((1, j, i))
            return 0
        return malloc_a_lot

    def test_llinterp_tuples(self):
        run, statistics = self.runner("llinterp_tuples", statistics=True)
        run([])
        heap_size = self.heap_usage(statistics)
        assert heap_size < 16000 * WORD / 4 # xxx

    def define_llinterp_dict(self):
        class A(object):
            pass
        def malloc_a_lot():
            i = 0
            while i < 10:
                i += 1
                a = (1, 2, i)
                b = {a: A()}
                j = 0
                while j < 20:
                    j += 1
                    b[1, j, i] = A()
            return 0
        return malloc_a_lot

    def test_llinterp_dict(self):
        run = self.runner("llinterp_dict")
        run([])

    def skipdefine_global_list(cls):
        gl = []
        class Box:
            def __init__(self):
                self.lst = gl
        box = Box()
        def append_to_list(i, j):
            box.lst.append([i] * 50)
            llop.gc__collect(lltype.Void)
            return box.lst[j][0]
        return append_to_list, None, None

    def test_global_list(self):
        py.test.skip("doesn't fit in the model, tested elsewhere too")
        run = self.runner("global_list")
        res = run([0, 0])
        assert res == 0
        for i in range(1, 5):
            res = run([i, i - 1])
            assert res == i - 1 # crashes if constants are not considered roots
            
    def define_string_concatenation(cls):
        def concat(j, dummy):
            lst = []
            for i in range(j):
                lst.append(str(i))
            return len("".join(lst))
        return concat

    def test_string_concatenation(self):
        run, statistics = self.runner("string_concatenation", statistics=True)
        res = run([100, 0])
        assert res == len(''.join([str(x) for x in range(100)]))
        heap_size = self.heap_usage(statistics)
        assert heap_size < 16000 * WORD / 4 # xxx

    def define_nongc_static_root(cls):
        T1 = lltype.GcStruct("C", ('x', lltype.Signed))
        T2 = lltype.Struct("C", ('p', lltype.Ptr(T1)))
        static = lltype.malloc(T2, immortal=True)
        def f():
            t1 = lltype.malloc(T1)
            t1.x = 42
            static.p = t1
            llop.gc__collect(lltype.Void)
            return static.p.x
        def cleanup():
            static.p = lltype.nullptr(T1)
        return f, cleanup, None

    def test_nongc_static_root(self):
        run = self.runner("nongc_static_root")
        res = run([])
        assert res == 42

    def define_finalizer(cls):
        class B(object):
            pass
        b = B()
        b.nextid = 0
        b.num_deleted = 0
        class A(object):
            def __init__(self):
                self.id = b.nextid
                b.nextid += 1
            def __del__(self):
                b.num_deleted += 1
        def f(x, y):
            a = A()
            i = 0
            while i < x:
                i += 1
                a = A()
            llop.gc__collect(lltype.Void)
            llop.gc__collect(lltype.Void)
            return b.num_deleted
        return f

    def test_finalizer(self):
        run = self.runner("finalizer")
        res = run([5, 42]) #XXX pure lazyness here too
        assert res == 6

    def define_finalizer_calls_malloc(cls):
        class B(object):
            pass
        b = B()
        b.nextid = 0
        b.num_deleted = 0
        class A(object):
            def __init__(self):
                self.id = b.nextid
                b.nextid += 1
            def __del__(self):
                b.num_deleted += 1
                C()
        class C(A):
            def __del__(self):
                b.num_deleted += 1
        def f(x, y):
            a = A()
            i = 0
            while i < x:
                i += 1
                a = A()
            llop.gc__collect(lltype.Void)
            llop.gc__collect(lltype.Void)
            return b.num_deleted
        return f

    def test_finalizer_calls_malloc(self):
        run = self.runner("finalizer_calls_malloc")
        res = run([5, 42]) #XXX pure lazyness here too
        assert res == 12

    def define_finalizer_resurrects(cls):
        class B(object):
            pass
        b = B()
        b.nextid = 0
        b.num_deleted = 0
        class A(object):
            def __init__(self):
                self.id = b.nextid
                b.nextid += 1
            def __del__(self):
                b.num_deleted += 1
                b.a = self
        def f(x, y):
            a = A()
            i = 0
            while i < x:
                i += 1
                a = A()
            llop.gc__collect(lltype.Void)
            llop.gc__collect(lltype.Void)
            aid = b.a.id
            b.a = None
            # check that __del__ is not called again
            llop.gc__collect(lltype.Void)
            llop.gc__collect(lltype.Void)
            return b.num_deleted * 10 + aid + 100 * (b.a is None)
        return f

    def test_finalizer_resurrects(self):
        run = self.runner("finalizer_resurrects")
        res = run([5, 42]) #XXX pure lazyness here too
        assert 160 <= res <= 165

    def define_custom_trace(cls):
        from pypy.rpython.annlowlevel import llhelper
        from pypy.rpython.lltypesystem import llmemory
        #
        S = lltype.GcStruct('S', ('x', llmemory.Address), rtti=True)
        T = lltype.GcStruct('T', ('z', lltype.Signed))
        offset_of_x = llmemory.offsetof(S, 'x')
        def customtrace(obj, prev):
            if not prev:
                return obj + offset_of_x
            else:
                return llmemory.NULL
        CUSTOMTRACEFUNC = lltype.FuncType([llmemory.Address, llmemory.Address],
                                          llmemory.Address)
        customtraceptr = llhelper(lltype.Ptr(CUSTOMTRACEFUNC), customtrace)
        lltype.attachRuntimeTypeInfo(S, customtraceptr=customtraceptr)
        #
        def setup():
            s1 = lltype.malloc(S)
            tx = lltype.malloc(T)
            tx.z = 4243
            s1.x = llmemory.cast_ptr_to_adr(tx)
            return s1
        def f():
            s1 = setup()
            llop.gc__collect(lltype.Void)
            return llmemory.cast_adr_to_ptr(s1.x, lltype.Ptr(T)).z
        return f

    def test_custom_trace(self):
        run = self.runner("custom_trace")
        res = run([])
        assert res == 4243

    def define_weakref(cls):
        import weakref, gc
        class A(object):
            pass
        def g():
            a = A()
            return weakref.ref(a)
        def f():
            a = A()
            ref = weakref.ref(a)
            result = ref() is a
            ref = g()
            llop.gc__collect(lltype.Void)
            result = result and (ref() is None)
            # check that a further collection is fine
            llop.gc__collect(lltype.Void)
            result = result and (ref() is None)
            return result
        return f

    def test_weakref(self):
        run = self.runner("weakref")
        res = run([])
        assert res

    def define_weakref_to_object_with_finalizer(cls):
        import weakref, gc
        class A(object):
            count = 0
        a = A()
        class B(object):
            def __del__(self):
                a.count += 1
        def g():
            b = B()
            return weakref.ref(b)
        def f():
            ref = g()
            llop.gc__collect(lltype.Void)
            llop.gc__collect(lltype.Void)
            result = a.count == 1 and (ref() is None)
            return result
        return f

    def test_weakref_to_object_with_finalizer(self):
        run = self.runner("weakref_to_object_with_finalizer")
        res = run([])
        assert res

    def define_collect_during_collect(cls):
        class B(object):
            pass
        b = B()
        b.nextid = 1
        b.num_deleted = 0
        b.num_deleted_c = 0
        class A(object):
            def __init__(self):
                self.id = b.nextid
                b.nextid += 1
            def __del__(self):
                llop.gc__collect(lltype.Void)
                b.num_deleted += 1
                C()
                C()
        class C(A):
            def __del__(self):
                b.num_deleted += 1
                b.num_deleted_c += 1
        def f(x, y):
            persistent_a1 = A()
            persistent_a2 = A()
            i = 0
            while i < x:
                i += 1
                a = A()
            persistent_a3 = A()
            persistent_a4 = A()
            llop.gc__collect(lltype.Void)
            llop.gc__collect(lltype.Void)
            b.bla = persistent_a1.id + persistent_a2.id + persistent_a3.id + persistent_a4.id
            # NB print would create a static root!
            llop.debug_print(lltype.Void, b.num_deleted_c)
            return b.num_deleted
        return f

    def test_collect_during_collect(self):
        run = self.runner("collect_during_collect")
        # runs collect recursively 4 times
        res = run([4, 42]) #XXX pure lazyness here too
        assert res == 12

    def define_collect_0(cls):
        def concat(j, dummy):
            lst = []
            for i in range(j):
                lst.append(str(i))
            result = len("".join(lst))
            if we_are_translated():
                llop.gc__collect(lltype.Void, 0)
            return result
        return concat

    def test_collect_0(self):
        run = self.runner("collect_0")
        res = run([100, 0])
        assert res == len(''.join([str(x) for x in range(100)]))

    def define_interior_ptrs(cls):
        from pypy.rpython.lltypesystem.lltype import Struct, GcStruct, GcArray
        from pypy.rpython.lltypesystem.lltype import Array, Signed, malloc

        S1 = Struct("S1", ('x', Signed))
        T1 = GcStruct("T1", ('s', S1))
        def f1():
            t = malloc(T1)
            t.s.x = 1
            return t.s.x

        S2 = Struct("S2", ('x', Signed))
        T2 = GcArray(S2)
        def f2():
            t = malloc(T2, 1)
            t[0].x = 1
            return t[0].x

        S3 = Struct("S3", ('x', Signed))
        T3 = GcStruct("T3", ('items', Array(S3)))
        def f3():
            t = malloc(T3, 1)
            t.items[0].x = 1
            return t.items[0].x

        S4 = Struct("S4", ('x', Signed))
        T4 = Struct("T4", ('s', S4))
        U4 = GcArray(T4)
        def f4():
            u = malloc(U4, 1)
            u[0].s.x = 1
            return u[0].s.x

        S5 = Struct("S5", ('x', Signed))
        T5 = GcStruct("T5", ('items', Array(S5)))
        def f5():
            t = malloc(T5, 1)
            return len(t.items)

        T6 = GcStruct("T6", ('s', Array(Signed)))
        def f6():
            t = malloc(T6, 1)
            t.s[0] = 1
            return t.s[0]

        def func():
            return (f1() * 100000 +
                    f2() * 10000 +
                    f3() * 1000 +
                    f4() * 100 +
                    f5() * 10 +
                    f6())

        assert func() == 111111
        return func

    def test_interior_ptrs(self):
        run = self.runner("interior_ptrs")
        res = run([])
        assert res == 111111

    def define_id(cls):
        class A(object):
            pass
        a1 = A()
        def func():
            a2 = A()
            a3 = A()
            id1 = compute_unique_id(a1)
            id2 = compute_unique_id(a2)
            id3 = compute_unique_id(a3)
            llop.gc__collect(lltype.Void)
            error = 0
            if id1 != compute_unique_id(a1): error += 1
            if id2 != compute_unique_id(a2): error += 2
            if id3 != compute_unique_id(a3): error += 4
            return error
        return func

    def test_id(self):
        run = self.runner("id")
        res = run([])
        assert res == 0

    def define_can_move(cls):
        TP = lltype.GcArray(lltype.Float)
        def func():
            return rgc.can_move(lltype.malloc(TP, 1))
        return func

    def test_can_move(self):
        run = self.runner("can_move")
        res = run([])
        assert res == self.GC_CAN_MOVE

    def define_malloc_nonmovable(cls):
        TP = lltype.GcArray(lltype.Char)
        def func():
            #try:
            a = rgc.malloc_nonmovable(TP, 3, zero=True)
            rgc.collect()
            if a:
                assert not rgc.can_move(a)
                return 1
            return 0
            #except Exception, e:
            #    return 2

        return func
    
    def test_malloc_nonmovable(self):
        run = self.runner("malloc_nonmovable")
        assert int(self.GC_CAN_MALLOC_NONMOVABLE) == run([])

    def define_malloc_nonmovable_fixsize(cls):
        S = lltype.GcStruct('S', ('x', lltype.Float))
        TP = lltype.GcStruct('T', ('s', lltype.Ptr(S)))
        def func():
            try:
                a = rgc.malloc_nonmovable(TP)
                rgc.collect()
                if a:
                    assert not rgc.can_move(a)
                    return 1
                return 0
            except Exception, e:
                return 2

        return func
    
    def test_malloc_nonmovable_fixsize(self):
        run = self.runner("malloc_nonmovable_fixsize")
        assert run([]) == int(self.GC_CAN_MALLOC_NONMOVABLE)

    def define_shrink_array(cls):
        from pypy.rpython.lltypesystem.rstr import STR

        def f():
            ptr = lltype.malloc(STR, 3)
            ptr.hash = 0x62
            ptr.chars[0] = '0'
            ptr.chars[1] = 'B'
            ptr.chars[2] = 'C'
            ptr2 = rgc.ll_shrink_array(ptr, 2)
            return ((ptr == ptr2)             +
                     ord(ptr2.chars[0])       +
                    (ord(ptr2.chars[1]) << 8) +
                    (len(ptr2.chars)   << 16) +
                    (ptr2.hash         << 24))
        return f

    def test_shrink_array(self):
        run = self.runner("shrink_array")
        if self.GC_CAN_SHRINK_ARRAY:
            expected = 0x62024231
        else:
            expected = 0x62024230
        assert run([]) == expected

    def define_string_builder_over_allocation(cls):
        import gc
        def fn():
            s = StringBuilder(4)
            s.append("abcd")
            s.append("defg")
            s.append("rty")
            s.append_multiple_char('y', 1000)
            gc.collect()
            s.append_multiple_char('y', 1000)
            res = s.build()[1000]
            gc.collect()
            return ord(res)
        return fn

    def test_string_builder_over_allocation(self):
        fn = self.runner("string_builder_over_allocation")
        res = fn([])
        assert res == ord('y')

class GenericMovingGCTests(GenericGCTests):
    GC_CAN_MOVE = True
    GC_CAN_MALLOC_NONMOVABLE = False
    GC_CAN_TEST_ID = False

    def define_many_ids(cls):
        class A(object):
            pass
        def f():
            from pypy.rpython.lltypesystem import rffi
            alist = [A() for i in range(50)]
            idarray = lltype.malloc(rffi.LONGP.TO, len(alist), flavor='raw')
            # Compute the id of all the elements of the list.  The goal is
            # to not allocate memory, so that if the GC needs memory to
            # remember the ids, it will trigger some collections itself
            i = 0
            while i < len(alist):
                idarray[i] = compute_unique_id(alist[i])
                i += 1
            j = 0
            while j < 2:
                if j == 1:     # allocate some stuff between the two iterations
                    [A() for i in range(20)]
                i = 0
                while i < len(alist):
                    assert idarray[i] == compute_unique_id(alist[i])
                    i += 1
                j += 1
            lltype.free(idarray, flavor='raw')
            return 0
        return f
    
    def test_many_ids(self):
        if not self.GC_CAN_TEST_ID:
            py.test.skip("fails for bad reasons in lltype.py :-(")
        run = self.runner("many_ids")
        run([])

    @classmethod
    def ensure_layoutbuilder(cls, translator):
        jit2gc = getattr(translator, '_jit2gc', None)
        if jit2gc:
            return jit2gc['layoutbuilder']
        GCClass = cls.gcpolicy.transformerclass.GCClass
        layoutbuilder = framework.TransformerLayoutBuilder(translator, GCClass)
        layoutbuilder.delay_encoding()
        translator._jit2gc = {
            'layoutbuilder': layoutbuilder,
        }
        return layoutbuilder

    def define_do_malloc_operations(cls):
        P = lltype.GcStruct('P', ('x', lltype.Signed))
        def g():
            r = lltype.malloc(P)
            r.x = 1
            p = llop.do_malloc_fixedsize_clear(llmemory.GCREF)  # placeholder
            p = lltype.cast_opaque_ptr(lltype.Ptr(P), p)
            p.x = r.x
            return p.x
        def f():
            i = 0
            while i < 40:
                g()
                i += 1
            return 0
        def fix_graph_of_g(translator):
            from pypy.translator.translator import graphof
            from pypy.objspace.flow.model import Constant
            from pypy.rpython.lltypesystem import rffi
            layoutbuilder = cls.ensure_layoutbuilder(translator)

            type_id = layoutbuilder.get_type_id(P)
            #
            # now fix the do_malloc_fixedsize_clear in the graph of g
            graph = graphof(translator, g)
            for op in graph.startblock.operations:
                if op.opname == 'do_malloc_fixedsize_clear':
                    op.args = [Constant(type_id, llgroup.HALFWORD),
                               Constant(llmemory.sizeof(P), lltype.Signed),
                               Constant(False, lltype.Bool), # has_finalizer
                               Constant(False, lltype.Bool)] # contains_weakptr
                    break
            else:
                assert 0, "oups, not found"
        return f, None, fix_graph_of_g
            
    def test_do_malloc_operations(self):
        run = self.runner("do_malloc_operations")
        run([])

    def define_do_malloc_operations_in_call(cls):
        P = lltype.GcStruct('P', ('x', lltype.Signed))
        def g():
            llop.do_malloc_fixedsize_clear(llmemory.GCREF)  # placeholder
        def f():
            q = lltype.malloc(P)
            q.x = 1
            i = 0
            while i < 40:
                g()
                i += q.x
            return 0
        def fix_graph_of_g(translator):
            from pypy.translator.translator import graphof
            from pypy.objspace.flow.model import Constant
            from pypy.rpython.lltypesystem import rffi
            layoutbuilder = cls.ensure_layoutbuilder(translator)            
            type_id = layoutbuilder.get_type_id(P)
            #
            # now fix the do_malloc_fixedsize_clear in the graph of g
            graph = graphof(translator, g)
            for op in graph.startblock.operations:
                if op.opname == 'do_malloc_fixedsize_clear':
                    op.args = [Constant(type_id, llgroup.HALFWORD),
                               Constant(llmemory.sizeof(P), lltype.Signed),
                               Constant(False, lltype.Bool), # has_finalizer
                               Constant(False, lltype.Bool)] # contains_weakptr
                    break
            else:
                assert 0, "oups, not found"
        return f, None, fix_graph_of_g
        
    def test_do_malloc_operations_in_call(self):
        run = self.runner("do_malloc_operations_in_call")
        run([])

    def define_gc_heap_stats(cls):
        S = lltype.GcStruct('S', ('x', lltype.Signed))
        l1 = []
        l2 = []
        l3 = []
        l4 = []
        
        def f():
            for i in range(10):
                s = lltype.malloc(S)
                l1.append(s)
                l2.append(s)
                if i < 3:
                    l3.append(s)
                    l4.append(s)
            # We cheat here and only read the table which we later on
            # process ourselves, otherwise this test takes ages
            llop.gc__collect(lltype.Void)
            tb = rgc._heap_stats()
            a = 0
            nr = 0
            b = 0
            c = 0
            d = 0
            e = 0
            for i in range(len(tb)):
                if tb[i].count == 10:
                    a += 1
                    nr = i
                if tb[i].count > 50:
                    d += 1
            for i in range(len(tb)):
                if tb[i].count == 4:
                    b += 1
                    c += tb[i].links[nr]
                    e += tb[i].size
            return d * 1000 + c * 100 + b * 10 + a
        return f

    def test_gc_heap_stats(self):
        py.test.skip("this test makes the following test crash.  Investigate.")
        run = self.runner("gc_heap_stats")
        res = run([])
        assert res % 10000 == 2611
        totsize = (res / 10000)
        size_of_int = rffi.sizeof(lltype.Signed)
        assert (totsize - 26 * size_of_int) % 4 == 0
        # ^^^ a crude assumption that totsize - varsize would be dividable by 4
        #     (and give fixedsize)

    def define_writebarrier_before_copy(cls):
        S = lltype.GcStruct('S', ('x', lltype.Char))
        TP = lltype.GcArray(lltype.Ptr(S))
        def fn():
            l = lltype.malloc(TP, 100)
            l2 = lltype.malloc(TP, 100)
            for i in range(100):
                l[i] = lltype.malloc(S)
            rgc.ll_arraycopy(l, l2, 50, 0, 50)
            # force nursery collect
            x = []
            for i in range(20):
                x.append((1, lltype.malloc(S)))
            for i in range(50):
                assert l2[i] == l[50 + i]
            return 0

        return fn

    def test_writebarrier_before_copy(self):
        run = self.runner("writebarrier_before_copy")
        run([])

# ________________________________________________________________

class TestMarkSweepGC(GenericGCTests):
    gcname = "marksweep"
    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            GC_PARAMS = {'start_heap_size': 1024*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200


class TestPrintingGC(GenericGCTests):
    gcname = "statistics"

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.marksweep import PrintingMarkSweepGC as GCClass
            GC_PARAMS = {'start_heap_size': 1024*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200

class TestSemiSpaceGC(GenericMovingGCTests):
    gcname = "semispace"
    GC_CAN_SHRINK_ARRAY = True

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.semispace import SemiSpaceGC as GCClass
            GC_PARAMS = {'space_size': 512*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200

class TestMarkCompactGC(GenericMovingGCTests):
    gcname = 'markcompact'

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.markcompact import MarkCompactGC as GCClass
            GC_PARAMS = {'space_size': 4096*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200

class TestGenerationGC(GenericMovingGCTests):
    gcname = "generation"
    GC_CAN_SHRINK_ARRAY = True

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.generation import GenerationGC as \
                                                          GCClass
            GC_PARAMS = {'space_size': 512*WORD,
                         'nursery_size': 32*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200

    def define_weakref_across_minor_collection(cls):
        import weakref
        class A:
            pass
        def f():
            x = 20    # for GenerationGC, enough for a minor collection
            a = A()
            a.foo = x
            ref = weakref.ref(a)
            all = [None] * x
            i = 0
            while i < x:
                all[i] = [i] * i
                i += 1
            assert ref() is a
            llop.gc__collect(lltype.Void)
            assert ref() is a
            return a.foo + len(all)
        return f

    def test_weakref_across_minor_collection(self):
        run = self.runner("weakref_across_minor_collection")
        res = run([])
        assert res == 20 + 20

    def define_nongc_static_root_minor_collect(cls):
        T1 = lltype.GcStruct("C", ('x', lltype.Signed))
        T2 = lltype.Struct("C", ('p', lltype.Ptr(T1)))
        static = lltype.malloc(T2, immortal=True)
        def f():
            t1 = lltype.malloc(T1)
            t1.x = 42
            static.p = t1
            x = 20
            all = [None] * x
            i = 0
            while i < x: # enough to cause a minor collect
                all[i] = [i] * i
                i += 1
            i = static.p.x
            llop.gc__collect(lltype.Void)
            return static.p.x + i
        def cleanup():
            static.p = lltype.nullptr(T1)        
        return f, cleanup, None

    def test_nongc_static_root_minor_collect(self):
        run = self.runner("nongc_static_root_minor_collect")
        res = run([])
        assert res == 84


    def define_static_root_minor_collect(cls):
        class A:
            pass
        class B:
            pass
        static = A()
        static.p = None
        def f():
            t1 = B()
            t1.x = 42
            static.p = t1
            x = 20
            all = [None] * x
            i = 0
            while i < x: # enough to cause a minor collect
                all[i] = [i] * i
                i += 1
            i = static.p.x
            llop.gc__collect(lltype.Void)
            return static.p.x + i
        def cleanup():
            static.p = None
        return f, cleanup, None

    def test_static_root_minor_collect(self):
        run = self.runner("static_root_minor_collect")
        res = run([])
        assert res == 84


    def define_many_weakrefs(cls):
        # test for the case where allocating the weakref itself triggers
        # a collection
        import weakref
        class A:
            pass
        def f():
            a = A()
            i = 0
            while i < 17:
                ref = weakref.ref(a)
                assert ref() is a
                i += 1
            return 0

        return f
        
    def test_many_weakrefs(self):
        run = self.runner("many_weakrefs")
        run([])

    def define_immutable_to_old_promotion(cls):
        T_CHILD = lltype.Ptr(lltype.GcStruct('Child', ('field', lltype.Signed)))
        T_PARENT = lltype.Ptr(lltype.GcStruct('Parent', ('sub', T_CHILD)))
        child = lltype.malloc(T_CHILD.TO)
        child2 = lltype.malloc(T_CHILD.TO)
        parent = lltype.malloc(T_PARENT.TO)
        parent2 = lltype.malloc(T_PARENT.TO)
        parent.sub = child
        child.field = 3
        parent2.sub = child2
        child2.field = 8

        T_ALL = lltype.Ptr(lltype.GcArray(T_PARENT))
        all = lltype.malloc(T_ALL.TO, 2)
        all[0] = parent
        all[1] = parent2

        def f(x, y):
            res = all[x]
            #all[x] = lltype.nullptr(T_PARENT.TO)
            return res.sub.field

        return f

    def test_immutable_to_old_promotion(self):
        run, transformer = self.runner("immutable_to_old_promotion", transformer=True)
        run([1, 4])
        if not transformer.GCClass.prebuilt_gc_objects_are_static_roots:
            assert len(transformer.layoutbuilder.addresses_of_static_ptrs) == 0
        else:
            assert len(transformer.layoutbuilder.addresses_of_static_ptrs) >= 4
        # NB. Remember that the number above does not count
        # the number of prebuilt GC objects, but the number of locations
        # within prebuilt GC objects that are of type Ptr(Gc).
        # At the moment we get additional_roots_sources == 6:
        #  * all[0]
        #  * all[1]
        #  * parent.sub
        #  * parent2.sub
        #  * the GcArray pointer from gc.wr_to_objects_with_id
        #  * the GcArray pointer from gc.object_id_dict.

    def define_adr_of_nursery(cls):
        class A(object):
            pass
        
        def f():
            # we need at least 1 obj to allocate a nursery
            a = A()
            nf_a = llop.gc_adr_of_nursery_free(llmemory.Address)
            nt_a = llop.gc_adr_of_nursery_top(llmemory.Address)
            nf0 = nf_a.address[0]
            nt0 = nt_a.address[0]
            a0 = A()
            a1 = A()
            nf1 = nf_a.address[0]
            nt1 = nt_a.address[0]
            assert nf1 > nf0
            assert nt1 > nf1
            assert nt1 == nt0
            return 0
        
        return f
    
    def test_adr_of_nursery(self):
        run = self.runner("adr_of_nursery")
        res = run([])        

class TestGenerationalNoFullCollectGC(GCTest):
    # test that nursery is doing its job and that no full collection
    # is needed when most allocated objects die quickly

    gcname = "generation"

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.generation import GenerationGC
            class GCClass(GenerationGC):
                __ready = False
                def setup(self):
                    from pypy.rpython.memory.gc.generation import GenerationGC
                    GenerationGC.setup(self)
                    self.__ready = True
                def semispace_collect(self, size_changing=False):
                    ll_assert(not self.__ready,
                              "no full collect should occur in this test")
            def _teardown(self):
                self.__ready = False # collecting here is expected
                GenerationGC._teardown(self)
                
            GC_PARAMS = {'space_size': 512*WORD,
                         'nursery_size': 128*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200

    def define_working_nursery(cls):
        def f():
            total = 0
            i = 0
            while i < 40:
                lst = []
                j = 0
                while j < 5:
                    lst.append(i*j)
                    j += 1
                total += len(lst)
                i += 1
            return total
        return f

    def test_working_nursery(self):
        run = self.runner("working_nursery")
        res = run([])
        assert res == 40 * 5

class TestHybridGC(TestGenerationGC):
    gcname = "hybrid"
    GC_CAN_MALLOC_NONMOVABLE = True

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.hybrid import HybridGC as GCClass
            GC_PARAMS = {'space_size': 512*WORD,
                         'nursery_size': 32*WORD,
                         'large_object': 8*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200

    def define_ref_from_rawmalloced_to_regular(cls):
        import gc
        S = lltype.GcStruct('S', ('x', lltype.Signed))
        A = lltype.GcStruct('A', ('p', lltype.Ptr(S)),
                                 ('a', lltype.Array(lltype.Char)))
        def setup(j):
            p = lltype.malloc(S)
            p.x = j*2
            lst = lltype.malloc(A, j)
            # the following line generates a write_barrier call at the moment,
            # which is important because the 'lst' can be allocated directly
            # in generation 2.  This can only occur with varsized mallocs.
            lst.p = p
            return lst
        def f(i, j):
            lst = setup(j)
            gc.collect()
            return lst.p.x
        return f

    def test_ref_from_rawmalloced_to_regular(self):
        run = self.runner("ref_from_rawmalloced_to_regular")
        res = run([100, 100])
        assert res == 200

    def define_assume_young_pointers(cls):
        from pypy.rlib import rgc
        S = lltype.GcForwardReference()
        S.become(lltype.GcStruct('S',
                                 ('x', lltype.Signed),
                                 ('prev', lltype.Ptr(S)),
                                 ('next', lltype.Ptr(S))))
        s0 = lltype.malloc(S, immortal=True)
        def f():
            s = lltype.malloc(S)
            s.x = 42
            llop.bare_setfield(lltype.Void, s0, void('next'), s)
            llop.gc_assume_young_pointers(lltype.Void,
                                          llmemory.cast_ptr_to_adr(s0))
            rgc.collect(0)
            return s0.next.x

        def cleanup():
            s0.next = lltype.nullptr(S)

        return f, cleanup, None

    def test_assume_young_pointers(self):
        run = self.runner("assume_young_pointers")
        res = run([])
        assert res == 42

    def test_malloc_nonmovable_fixsize(self):
        py.test.skip("not supported")


class TestMiniMarkGC(TestHybridGC):
    gcname = "minimark"
    GC_CAN_TEST_ID = True

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.minimark import MiniMarkGC as GCClass
            GC_PARAMS = {'nursery_size': 32*WORD,
                         'page_size': 16*WORD,
                         'arena_size': 64*WORD,
                         'small_request_threshold': 5*WORD,
                         'large_object': 8*WORD,
                         'card_page_indices': 4,
                         'translated_to_c': False,
                         }
            root_stack_depth = 200

    def define_no_clean_setarrayitems(cls):
        # The optimization find_clean_setarrayitems() in
        # gctransformer/framework.py does not work with card marking.
        # Check that it is turned off.
        S = lltype.GcStruct('S', ('x', lltype.Signed))
        A = lltype.GcArray(lltype.Ptr(S))
        def sub(lst):
            lst[15] = lltype.malloc(S)   # 'lst' is set the single mark "12-15"
            lst[15].x = 123
            lst[0] = lst[15]   # that would be a "clean_setarrayitem"
        def f():
            lst = lltype.malloc(A, 16)   # 16 > 10
            rgc.collect()
            sub(lst)
            null = lltype.nullptr(S)
            lst[15] = null     # clear, so that A() is only visible via lst[0]
            rgc.collect()      # -> crash
            return lst[0].x
        return f

    def test_no_clean_setarrayitems(self):
        run = self.runner("no_clean_setarrayitems")
        res = run([])
        assert res == 123

# ________________________________________________________________
# tagged pointers

class TaggedPointerGCTests(GCTest):
    taggedpointers = True

    def define_tagged_simple(cls):
        class Unrelated(object):
            pass

        u = Unrelated()
        u.x = UnboxedObject(47)
        def fn(n):
            rgc.collect() # check that a prebuilt tagged pointer doesn't explode
            if n > 0:
                x = BoxedObject(n)
            else:
                x = UnboxedObject(n)
            u.x = x # invoke write barrier
            rgc.collect()
            return x.meth(100)
        def func():
            return fn(1000) + fn(-1000)
        assert func() == 205
        return func

    def test_tagged_simple(self):
        func = self.runner("tagged_simple")
        res = func([])
        assert res == 205

    def define_tagged_prebuilt(cls):

        class F:
            pass

        f = F()
        f.l = [UnboxedObject(10)]
        def fn(n):
            if n > 0:
                x = BoxedObject(n)
            else:
                x = UnboxedObject(n)
            f.l.append(x)
            rgc.collect()
            return f.l[-1].meth(100)
        def func():
            return fn(1000) ^ fn(-1000)
        assert func() == -1999
        return func

    def test_tagged_prebuilt(self):
        func = self.runner("tagged_prebuilt")
        res = func([])
        assert res == -1999

from pypy.rlib.objectmodel import UnboxedValue

class TaggedBase(object):
    __slots__ = ()
    def meth(self, x):
        raise NotImplementedError

class BoxedObject(TaggedBase):
    attrvalue = 66
    def __init__(self, normalint):
        self.normalint = normalint
    def meth(self, x):
        return self.normalint + x + 2

class UnboxedObject(TaggedBase, UnboxedValue):
    __slots__ = 'smallint'
    def meth(self, x):
        return self.smallint + x + 3


class TestMarkSweepTaggedPointerGC(TaggedPointerGCTests):
    gcname = "marksweep"
    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            GC_PARAMS = {'start_heap_size': 1024*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200

class TestHybridTaggedPointerGC(TaggedPointerGCTests):
    gcname = "hybrid"

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.generation import GenerationGC as \
                                                          GCClass
            GC_PARAMS = {'space_size': 512*WORD,
                         'nursery_size': 32*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200

class TestMarkCompactTaggedpointerGC(TaggedPointerGCTests):
    gcname = 'markcompact'

    class gcpolicy(gc.FrameworkGcPolicy):
        class transformerclass(framework.FrameworkGCTransformer):
            from pypy.rpython.memory.gc.markcompact import MarkCompactGC as GCClass
            GC_PARAMS = {'space_size': 4096*WORD,
                         'translated_to_c': False}
            root_stack_depth = 200
