import py
import sys, os, inspect

from pypy.objspace.flow.model import summary
from pypy.rpython.lltypesystem import lltype, llmemory
from pypy.rpython.lltypesystem.lloperation import llop
from pypy.rpython.memory.test import snippet
from pypy.rlib import rgc
from pypy.rlib.objectmodel import keepalive_until_here
from pypy.rlib.rstring import StringBuilder, UnicodeBuilder
from pypy.tool.udir import udir
from pypy.translator.interactive import Translation
from pypy.annotation import policy as annpolicy
from pypy import conftest

class TestUsingFramework(object):
    gcpolicy = "marksweep"
    should_be_moving = False
    removetypeptr = False
    taggedpointers = False
    GC_CAN_MOVE = False
    GC_CAN_MALLOC_NONMOVABLE = True
    GC_CAN_SHRINK_ARRAY = False

    _isolated_func = None
    c_allfuncs = None

    @classmethod
    def _makefunc_str_int(cls, f):
        def main(argv):
            arg0 = argv[1]
            arg1 = int(argv[2])
            try:
                res = f(arg0, arg1)
            except MemoryError:
                print "MEMORY-ERROR"
            else:
                print res
            return 0
        
        t = Translation(main, standalone=True, gc=cls.gcpolicy,
                        policy=annpolicy.StrictAnnotatorPolicy(),
                        taggedpointers=cls.taggedpointers,
                        gcremovetypeptr=cls.removetypeptr)
        t.disable(['backendopt'])
        t.set_backend_extra_options(c_debug_defines=True)
        t.rtype()
        if conftest.option.view:
            t.viewcg()
        exename = t.compile()

        def run(s, i):
            data = py.process.cmdexec("%s %s %d" % (exename, s, i))
            data = data.strip()
            if data == 'MEMORY-ERROR':
                raise MemoryError
            return data

        return run


    def setup_class(cls):
        funcs0 = []
        funcs1 = []
        funcsstr = []
        name_to_func = {}
        for fullname in dir(cls):
            if not fullname.startswith('define'):
                continue
            keyword = conftest.option.keyword
            if keyword.startswith('test_'):
                keyword = keyword[len('test_'):]
                if keyword not in fullname:
                    continue
            prefix, name = fullname.split('_', 1)
            definefunc = getattr(cls, fullname)
            func = definefunc.im_func(cls)
            func.func_name = 'f_'+name
            if prefix == 'definestr':
                funcsstr.append(func)
                funcs0.append(None)
                funcs1.append(None)
            else:            
                numargs = len(inspect.getargspec(func)[0])
                funcsstr.append(None)
                if numargs == 0:
                    funcs0.append(func)
                    funcs1.append(None)
                else:
                    assert numargs == 1
                    funcs0.append(None)
                    funcs1.append(func)
            assert name not in name_to_func
            name_to_func[name] = len(name_to_func)
        def allfuncs(name, arg):
            num = name_to_func[name]
            func0 = funcs0[num]
            if func0:
                return str(func0())
            func1 = funcs1[num]
            if func1:
                return str(func1(arg))
            funcstr = funcsstr[num]
            if funcstr:
                return funcstr(arg)
            assert 0, 'unreachable'
        cls.funcsstr = funcsstr
        cls.c_allfuncs = staticmethod(cls._makefunc_str_int(allfuncs))
        cls.allfuncs = staticmethod(allfuncs)
        cls.name_to_func = name_to_func

    def teardown_class(cls):
        if hasattr(cls.c_allfuncs, 'close_isolate'):
            cls.c_allfuncs.close_isolate()
            cls.c_allfuncs = None

    def run(self, name, *args):
        if not args:
            args = (-1, )
        print 'Running %r)' % name
        res = self.c_allfuncs(name, *args)
        num = self.name_to_func[name]
        if self.funcsstr[num]:
            return res
        return int(res)

    def run_orig(self, name, *args):
        if not args:
            args = (-1, )
        res = self.allfuncs(name, *args)
        num = self.name_to_func[name]        
        if self.funcsstr[num]:
            return res
        return int(res)        

    def define_empty_collect(cls):
        def f():
            llop.gc__collect(lltype.Void)
            return 41
        return f

    def test_empty_collect(self):
        res = self.run('empty_collect')
        assert res == 41

    def define_framework_simple(cls):
        def g(x): # cannot cause a collect
            return x + 1
        class A(object):
            pass
        def make():
            a = A()
            a.b = g(1)
            return a
        make._dont_inline_ = True
        def f():
            a = make()
            llop.gc__collect(lltype.Void)
            return a.b
        return f

    def test_framework_simple(self):
        res = self.run('framework_simple')
        assert res == 2

    def define_framework_safe_pushpop(cls):
        class A(object):
            pass
        class B(object):
            pass
        def g(x): # cause a collect
            llop.gc__collect(lltype.Void)
        g._dont_inline_ = True
        global_a = A()
        global_a.b = B()
        global_a.b.a = A()
        global_a.b.a.b = B()
        global_a.b.a.b.c = 1
        def make():
            global_a.b.a.b.c = 40
            a = global_a.b.a
            b = a.b
            b.c = 41
            g(1)
            b0 = a.b
            b0.c = b.c = 42
        make._dont_inline_ = True
        def f():
            make()
            llop.gc__collect(lltype.Void)
            return global_a.b.a.b.c
        return f

    def test_framework_safe_pushpop(self):
        res = self.run('framework_safe_pushpop')
        assert res == 42

    def define_framework_protect_getfield(cls):
        class A(object):
            pass
        class B(object):
            pass
        def prepare(b, n):
            a = A()
            a.value = n
            b.a = a
            b.othervalue = 5
        def g(a):
            llop.gc__collect(lltype.Void)
            for i in range(1000):
                prepare(B(), -1)    # probably overwrites collected memory
            return a.value
        g._dont_inline_ = True
        def f():
            b = B()
            prepare(b, 123)
            a = b.a
            b.a = None
            return g(a) + b.othervalue
        return f

    def test_framework_protect_getfield(self):
        res = self.run('framework_protect_getfield')
        assert res == 128

    def define_framework_varsized(cls):
        S = lltype.GcStruct("S", ('x', lltype.Signed))
        T = lltype.GcStruct("T", ('y', lltype.Signed),
                                 ('s', lltype.Ptr(S)))
        ARRAY_Ts = lltype.GcArray(lltype.Ptr(T))
        
        def f():
            r = 0
            for i in range(30):
                a = lltype.malloc(ARRAY_Ts, i)
                for j in range(i):
                    a[j] = lltype.malloc(T)
                    a[j].y = i
                    a[j].s = lltype.malloc(S)
                    a[j].s.x = 2*i
                    r += a[j].y + a[j].s.x
                    a[j].s = lltype.malloc(S)
                    a[j].s.x = 3*i
                    r -= a[j].s.x
                for j in range(i):
                    r += a[j].y
            return r
        return f

    def test_framework_varsized(self):
        res = self.run('framework_varsized')
        assert res == self.run_orig('framework_varsized')
            
    def define_framework_using_lists(cls):
        class A(object):
            pass
        N = 1000
        def f():
            static_list = []
            for i in range(N):
                a = A()
                a.x = i
                static_list.append(a)
            r = 0
            for a in static_list:
                r += a.x
            return r
        return f

    def test_framework_using_lists(self):
        N = 1000
        res = self.run('framework_using_lists')
        assert res == N*(N - 1)/2
    
    def define_framework_static_roots(cls):
        class A(object):
            def __init__(self, y):
                self.y = y
        a = A(0)
        a.x = None
        def make():
            a.x = A(42)
        make._dont_inline_ = True
        def f():
            make()
            llop.gc__collect(lltype.Void)
            return a.x.y
        return f

    def test_framework_static_roots(self):
        res = self.run('framework_static_roots')
        assert res == 42

    def define_framework_nongc_static_root(cls):
        S = lltype.GcStruct("S", ('x', lltype.Signed))
        T = lltype.Struct("T", ('p', lltype.Ptr(S)))
        t = lltype.malloc(T, immortal=True)
        def f():
            t.p = lltype.malloc(S)
            t.p.x = 43
            for i in range(2500000):
                s = lltype.malloc(S)
                s.x = i
            return t.p.x
        return f

    def test_framework_nongc_static_root(self):
        res = self.run('framework_nongc_static_root')
        assert res == 43

    def define_framework_void_array(cls):
        A = lltype.GcArray(lltype.Void)
        a = lltype.malloc(A, 44)
        def f():
            return len(a)
        return f

    def test_framework_void_array(self):
        res = self.run('framework_void_array')
        assert res == 44
        
        
    def define_framework_malloc_failure(cls):
        def f():
            a = [1] * (sys.maxint//2)
            return len(a) + a[0]
        return f

    def test_framework_malloc_failure(self):
        py.test.raises(MemoryError, self.run, 'framework_malloc_failure')

    def define_framework_array_of_void(cls):
        def f():
            a = [None] * 43
            b = []
            for i in range(1000000):
                a.append(None)
                b.append(len(a))
            return b[-1]
        return f

    def test_framework_array_of_void(self):
        res = self.run('framework_array_of_void')
        assert res == 43 + 1000000
        
    def define_framework_opaque(cls):
        A = lltype.GcStruct('A', ('value', lltype.Signed))
        O = lltype.GcOpaqueType('test.framework')

        def gethidden(n):
            a = lltype.malloc(A)
            a.value = -n * 7
            return lltype.cast_opaque_ptr(lltype.Ptr(O), a)
        gethidden._dont_inline_ = True
        def reveal(o):
            return lltype.cast_opaque_ptr(lltype.Ptr(A), o)
        def overwrite(a, i):
            a.value = i
        overwrite._dont_inline_ = True
        def f():
            o = gethidden(10)
            llop.gc__collect(lltype.Void)
            for i in range(1000):    # overwrite freed memory
                overwrite(lltype.malloc(A), i)
            a = reveal(o)
            return a.value
        return f

    def test_framework_opaque(self):
        res = self.run('framework_opaque')
        assert res == -70

    def define_framework_finalizer(cls):
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
        def f():
            a = A()
            i = 0
            while i < 5:
                i += 1
                a = A()
            llop.gc__collect(lltype.Void)
            llop.gc__collect(lltype.Void)
            return b.num_deleted
        return f

    def test_framework_finalizer(self):
        res = self.run('framework_finalizer')
        assert res == 6

    def define_del_catches(cls):
        import os
        def g():
            pass
        class A(object):
            def __del__(self):
                try:
                    g()
                except:
                    os.write(1, "hallo")
        def f1(i):
            if i:
                raise TypeError
        def f(i):
            a = A()
            f1(i)
            a.b = 1
            llop.gc__collect(lltype.Void)
            return a.b
        def h(x):
            try:
                return f(x)
            except TypeError:
                return 42
        return h

    def test_del_catches(self):
        res = self.run('del_catches', 0)
        assert res == 1
        res = self.run('del_catches', 1)
        assert res == 42

    def define_del_raises(cls):
        class B(object):
            def __del__(self):
                raise TypeError
        def func():
            b = B()
            return 0
        return func
    
    def test_del_raises(self):
        self.run('del_raises') # does not raise

    def define_custom_trace(cls):
        from pypy.rpython.annlowlevel import llhelper
        from pypy.rpython.lltypesystem import llmemory
        #
        S = lltype.GcStruct('S', ('x', llmemory.Address), rtti=True)
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
            s = lltype.nullptr(S)
            for i in range(10000):
                t = lltype.malloc(S)
                t.x = llmemory.cast_ptr_to_adr(s)
                s = t
            return s
        def measure_length(s):
            res = 0
            while s:
                res += 1
                s = llmemory.cast_adr_to_ptr(s.x, lltype.Ptr(S))
            return res
        def f(n):
            s1 = setup()
            llop.gc__collect(lltype.Void)
            return measure_length(s1)
        return f

    def test_custom_trace(self):
        res = self.run('custom_trace', 0)
        assert res == 10000

    def define_weakref(cls):
        import weakref

        class A:
            pass

        keepalive = []
        def fn():
            n = 7000
            weakrefs = []
            a = None
            for i in range(n):
                if i & 1 == 0:
                    a = A()
                    a.index = i
                assert a is not None
                weakrefs.append(weakref.ref(a))
                if i % 7 == 6:
                    keepalive.append(a)
            rgc.collect()
            count_free = 0
            for i in range(n):
                a = weakrefs[i]()
                if i % 7 == 6:
                    assert a is not None
                if a is not None:
                    assert a.index == i & ~1
                else:
                    count_free += 1
            return count_free
        return fn

    def test_weakref(self):
        res = self.run('weakref')
        # more than half of them should have been freed, ideally up to 6000
        assert 3500 <= res <= 6000

    def define_prebuilt_weakref(cls):
        import weakref
        class A:
            pass
        a = A()
        a.hello = 42
        refs = [weakref.ref(a), weakref.ref(A())]
        rgc.collect()
        def fn():
            result = 0
            for i in range(2):
                a = refs[i]()
                rgc.collect()
                if a is None:
                    result += (i+1)
                else:
                    result += a.hello * (i+1)
            return result
        return fn

    def test_prebuilt_weakref(self):
        res = self.run('prebuilt_weakref')
        assert res == self.run_orig('prebuilt_weakref')

    def define_framework_malloc_raw(cls):
        A = lltype.Struct('A', ('value', lltype.Signed))

        def f():
            p = lltype.malloc(A, flavor='raw')
            p.value = 123
            llop.gc__collect(lltype.Void)
            res = p.value
            lltype.free(p, flavor='raw')
            return res
        return f

    def test_framework_malloc_raw(self):
        res = self.run('framework_malloc_raw')
        assert res == 123

    def define_framework_del_seeing_new_types(cls):
        class B(object):
            pass
        class A(object):
            def __del__(self):
                B()
        def f():
            A()
            return 42
        return f

    def test_framework_del_seeing_new_types(self):
        res = self.run('framework_del_seeing_new_types')
        assert res == 42

    def define_framework_late_filling_pointers(cls):
        A = lltype.GcStruct('A', ('x', lltype.Signed))
        B = lltype.GcStruct('B', ('a', lltype.Ptr(A)))

        def f():
            p = lltype.malloc(B)
            llop.gc__collect(lltype.Void)
            p.a = lltype.malloc(A)
            return p.a.x
        return f

    def test_framework_late_filling_pointers(self):
        # the point is just not to segfault
        self.run('framework_late_filling_pointers')

    def define_zero_raw_malloc(cls):
        S = lltype.Struct('S', ('x', lltype.Signed), ('y', lltype.Signed))
        def f():
            for i in range(100):
                p = lltype.malloc(S, flavor='raw', zero=True)
                if p.x != 0 or p.y != 0:
                    return -1
                p.x = i
                p.y = i
                lltype.free(p, flavor='raw')
            return 42

        return f

    def test_zero_raw_malloc(self):
        res = self.run('zero_raw_malloc')
        assert res == 42

    def define_object_alignment(cls):
        # all objects returned by the GC should be properly aligned.
        from pypy.rpython.lltypesystem import rffi
        mylist = ['a', 'bc', '84139871', 'ajkdh', '876']
        def f():
            result = 0
            buffer = ""
            for j in range(100):
                for s in mylist:
                    buffer += s
                    addr = rffi.cast(lltype.Signed, buffer)
                    result |= addr
            return result

        return f

    def test_object_alignment(self):
        res = self.run('object_alignment')
        from pypy.rpython.tool import rffi_platform
        expected_alignment = rffi_platform.memory_alignment()
        assert (res & (expected_alignment-1)) == 0

    def define_void_list(cls):
        class E:
            def __init__(self):
                self.l = []
        def f():
            e = E()
            return len(e.l)
        return f

    def test_void_list(self):
        assert self.run('void_list') == 0

    filename = str(udir.join('test_open_read_write_close.txt'))
    def define_open_read_write_seek_close(cls):
        filename = cls.filename
        def does_stuff():
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT, 0777)
            count = os.write(fd, "hello world\n")
            assert count == len("hello world\n")
            os.close(fd)
            fd = os.open(filename, os.O_RDONLY, 0777)
            result = os.lseek(fd, 1, 0)
            assert result == 1
            data = os.read(fd, 500)
            assert data == "ello world\n"
            os.close(fd)
            return 0

        return does_stuff

    def test_open_read_write_seek_close(self):
        self.run('open_read_write_seek_close')
        assert open(self.filename, 'r').read() == "hello world\n"
        os.unlink(self.filename)

    def define_callback_with_collect(cls):
        from pypy.rlib.clibffi import ffi_type_pointer, cast_type_to_ffitype,\
             CDLL, ffi_type_void, CallbackFuncPtr, ffi_type_sint
        from pypy.rpython.lltypesystem import rffi, ll2ctypes
        import gc
        ffi_size_t = cast_type_to_ffitype(rffi.SIZE_T)

        from pypy.rlib.clibffi import get_libc_name

        def callback(ll_args, ll_res, stuff):
            gc.collect()
            p_a1 = rffi.cast(rffi.VOIDPP, ll_args[0])[0]
            p_a2 = rffi.cast(rffi.VOIDPP, ll_args[1])[0]
            a1 = rffi.cast(rffi.LONGP, p_a1)[0]
            a2 = rffi.cast(rffi.LONGP, p_a2)[0]
            res = rffi.cast(rffi.INTP, ll_res)
            if a1 > a2:
                res[0] = rffi.cast(rffi.INT, 1)
            else:
                res[0] = rffi.cast(rffi.INT, -1)

        def f():
            libc = CDLL(get_libc_name())
            qsort = libc.getpointer('qsort', [ffi_type_pointer, ffi_size_t,
                                              ffi_size_t, ffi_type_pointer],
                                    ffi_type_void)

            ptr = CallbackFuncPtr([ffi_type_pointer, ffi_type_pointer],
                                  ffi_type_sint, callback)

            TP = rffi.CArray(rffi.LONG)
            to_sort = lltype.malloc(TP, 4, flavor='raw')
            to_sort[0] = 4
            to_sort[1] = 3
            to_sort[2] = 1
            to_sort[3] = 2
            qsort.push_arg(rffi.cast(rffi.VOIDP, to_sort))
            qsort.push_arg(rffi.cast(rffi.SIZE_T, 4))
            qsort.push_arg(rffi.cast(rffi.SIZE_T, rffi.sizeof(rffi.LONG)))
            qsort.push_arg(rffi.cast(rffi.VOIDP, ptr.ll_closure))
            qsort.call(lltype.Void)
            result = [to_sort[i] for i in range(4)] == [1,2,3,4]
            lltype.free(to_sort, flavor='raw')
            keepalive_until_here(ptr)
            return int(result)

        return f

    def test_callback_with_collect(self):
        assert self.run('callback_with_collect')
    
    def define_can_move(cls):
        class A:
            pass
        def fn():
            return rgc.can_move(A())
        return fn

    def test_can_move(self):
        assert self.run('can_move') == self.GC_CAN_MOVE

    def define_malloc_nonmovable(cls):
        TP = lltype.GcArray(lltype.Char)
        def func():
            try:
                a = rgc.malloc_nonmovable(TP, 3)
                rgc.collect()
                if a:
                    assert not rgc.can_move(a)
                    return 1
                return 0
            except Exception, e:
                return 2

        return func

    def test_malloc_nonmovable(self):
        res = self.run('malloc_nonmovable')
        assert res == self.GC_CAN_MALLOC_NONMOVABLE

    def define_resizable_buffer(cls):
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

    def test_resizable_buffer(self):
        res = self.run('resizable_buffer')
        if self.GC_CAN_SHRINK_ARRAY:
            expected = 0x62024231
        else:
            expected = 0x62024230
        assert res == expected

    def define_hash_preservation(cls):
        from pypy.rlib.objectmodel import compute_hash
        from pypy.rlib.objectmodel import compute_identity_hash
        from pypy.rlib.objectmodel import current_object_addr_as_int
        class C:
            pass
        class D(C):
            pass
        c = C()
        d = D()
        h_d = compute_hash(d)     # force to be cached on 'd', but not on 'c'
        h_t = compute_hash(("Hi", None, (7.5, 2, d)))
        S = lltype.GcStruct('S', ('x', lltype.Signed),
                                 ('a', lltype.Array(lltype.Signed)))
        s = lltype.malloc(S, 15, zero=True)
        h_s = compute_identity_hash(s)   # varsized: hash not saved/restored
        #
        def f():
            if compute_hash(c) != compute_identity_hash(c): return 12
            if compute_hash(d) != h_d: return 13
            if compute_hash(("Hi", None, (7.5, 2, d))) != h_t: return 14
            c2 = C()
            h_c2 = compute_hash(c2)
            if compute_hash(c2) != h_c2: return 15
            if compute_identity_hash(s) == h_s: return 16   # unlikely
            i = 0
            while i < 6:
                rgc.collect()
                if compute_hash(c2) != h_c2: return i
                i += 1
            return 42
        return f

    def test_hash_preservation(self):
        res = self.run('hash_preservation')
        assert res == 42

    def define_hash_overflow(self):
        from pypy.rlib.objectmodel import compute_identity_hash
        class X(object):
            pass

        def g(n):
            "Make a chain of n objects."
            x1 = None
            i = 0
            while i < n:
                x2 = X()
                x2.prev = x1
                x1 = x2
                i += 1
            return x1

        def build(xr, n):
            "Build the identity hashes of all n objects of the chain."
            i = 0
            while i < n:
                xr.hash = compute_identity_hash(xr)
                # ^^^ likely to trigger a collection
                xr = xr.prev
                i += 1
            assert xr is None

        def check(xr, n, step):
            "Check that the identity hashes are still correct."
            i = 0
            while i < n:
                if xr.hash != compute_identity_hash(xr):
                    os.write(2, "wrong hash! i=%d, n=%d, step=%d\n" % (i, n,
                                                                       step))
                    raise ValueError
                xr = xr.prev
                i += 1
            assert xr is None

        def h(n):
            x3 = g(3)
            x4 = g(3)
            x1 = g(n)
            build(x1, n)       # can collect!
            check(x1, n, 1)
            build(x3, 3)
            x2 = g(n//2)       # allocate more and try again
            build(x2, n//2)
            check(x1, n, 11)
            check(x2, n//2, 12)
            build(x4, 3)
            check(x3, 3, 13)   # check these old objects too
            check(x4, 3, 14)   # check these old objects too
            rgc.collect()
            check(x1, n, 21)
            check(x2, n//2, 22)
            check(x3, 3, 23)
            check(x4, 3, 24)

        def f():
            # numbers optimized for a 8MB space
            for n in [100000, 225000, 250000, 300000, 380000,
                      460000, 570000, 800000]:
                os.write(2, 'case %d\n' % n)
                rgc.collect()
                h(n)
            return -42

        return f

    def test_hash_overflow(self):
        res = self.run('hash_overflow')
        assert res == -42

    def define_hash_varsized(self):
        S = lltype.GcStruct('S', ('abc', lltype.Signed),
                                 ('def', lltype.Array(lltype.Signed)))
        s = lltype.malloc(S, 3, zero=True)
        h_s = lltype.identityhash(s)
        def f():
            return lltype.identityhash(s) - h_s    # != 0 (so far),
                                # because S is a varsized structure.
        return f

    def test_hash_varsized(self):
        res = self.run('hash_varsized')
        assert res != 0


    def define_arraycopy_writebarrier_int(cls):
        TP = lltype.GcArray(lltype.Signed)
        S = lltype.GcStruct('S')
        def fn():
            l = lltype.malloc(TP, 100)
            for i in range(100):
                l[i] = i * 3
            l2 = lltype.malloc(TP, 50)
            rgc.ll_arraycopy(l, l2, 40, 0, 50)
            # force a nursery collect
            x = []
            for i in range(20):
                x.append((1, lltype.malloc(S)))
            for i in range(50):
                assert l2[i] == (40 + i) * 3
            return 0

        return fn

    def test_arraycopy_writebarrier_int(self):
        self.run("arraycopy_writebarrier_int")

    def define_arraycopy_writebarrier_ptr(cls):
        TP = lltype.GcArray(lltype.Ptr(lltype.GcArray(lltype.Signed)))
        def fn():
            l = lltype.malloc(TP, 100)
            for i in range(100):
                l[i] = lltype.malloc(TP.OF.TO, i)
            l2 = lltype.malloc(TP, 50)
            rgc.ll_arraycopy(l, l2, 40, 0, 50)
            rgc.collect()
            for i in range(50):
                assert l2[i] == l[40 + i]
            return 0

        return fn

    def test_arraycopy_writebarrier_ptr(self):
        self.run("arraycopy_writebarrier_ptr")

    def define_get_rpy_roots(self):
        U = lltype.GcStruct('U', ('x', lltype.Signed))
        S = lltype.GcStruct('S', ('u', lltype.Ptr(U)))

        def g(s):
            lst = rgc.get_rpy_roots()
            found = False
            for x in lst:
                if x == lltype.cast_opaque_ptr(llmemory.GCREF, s):
                    found = True
                if x == lltype.cast_opaque_ptr(llmemory.GCREF, s.u):
                    os.write(2, "s.u should not be found!\n")
                    assert False
            return found == 1

        def fn():
            s = lltype.malloc(S)
            s.u = lltype.malloc(U)
            found = g(s)
            if not found:
                os.write(2, "not found!\n")
                assert False
            s.u.x = 42
            return 0

        return fn

    def test_get_rpy_roots(self):
        self.run("get_rpy_roots")

    def define_get_rpy_referents(self):
        U = lltype.GcStruct('U', ('x', lltype.Signed))
        S = lltype.GcStruct('S', ('u', lltype.Ptr(U)))

        def fn():
            s = lltype.malloc(S)
            s.u = lltype.malloc(U)
            gcref1 = lltype.cast_opaque_ptr(llmemory.GCREF, s)
            gcref2 = lltype.cast_opaque_ptr(llmemory.GCREF, s.u)
            lst = rgc.get_rpy_referents(gcref1)
            assert gcref2 in lst
            assert gcref1 not in lst
            s.u.x = 42
            return 0

        return fn

    def test_get_rpy_referents(self):
        self.run("get_rpy_referents")

    def define_is_rpy_instance(self):
        class Foo:
            pass
        S = lltype.GcStruct('S', ('x', lltype.Signed))

        def check(gcref, expected):
            result = rgc._is_rpy_instance(gcref)
            assert result == expected

        def fn():
            s = lltype.malloc(S)
            gcref1 = lltype.cast_opaque_ptr(llmemory.GCREF, s)
            check(gcref1, False)

            f = Foo()
            gcref3 = rgc.cast_instance_to_gcref(f)
            check(gcref3, True)

            return 0

        return fn

    def test_is_rpy_instance(self):
        self.run("is_rpy_instance")

    def define_try_cast_gcref_to_instance(self):
        class Foo:
            pass
        class FooBar(Foo):
            pass
        class Biz(object):
            pass
        S = lltype.GcStruct('S', ('x', lltype.Signed))

        def fn():
            foo = Foo()
            gcref1 = rgc.cast_instance_to_gcref(foo)
            assert rgc.try_cast_gcref_to_instance(Foo,    gcref1) is foo
            assert rgc.try_cast_gcref_to_instance(FooBar, gcref1) is None
            assert rgc.try_cast_gcref_to_instance(Biz,    gcref1) is None

            foobar = FooBar()
            gcref2 = rgc.cast_instance_to_gcref(foobar)
            assert rgc.try_cast_gcref_to_instance(Foo,    gcref2) is foobar
            assert rgc.try_cast_gcref_to_instance(FooBar, gcref2) is foobar
            assert rgc.try_cast_gcref_to_instance(Biz,    gcref2) is None

            s = lltype.malloc(S)
            gcref3 = lltype.cast_opaque_ptr(llmemory.GCREF, s)
            assert rgc.try_cast_gcref_to_instance(Foo,    gcref3) is None
            assert rgc.try_cast_gcref_to_instance(FooBar, gcref3) is None
            assert rgc.try_cast_gcref_to_instance(Biz,    gcref3) is None

            return 0

        return fn

    def test_try_cast_gcref_to_instance(self):
        self.run("try_cast_gcref_to_instance")

    def define_get_rpy_memory_usage(self):
        U = lltype.GcStruct('U', ('x1', lltype.Signed),
                                 ('x2', lltype.Signed),
                                 ('x3', lltype.Signed),
                                 ('x4', lltype.Signed),
                                 ('x5', lltype.Signed),
                                 ('x6', lltype.Signed),
                                 ('x7', lltype.Signed),
                                 ('x8', lltype.Signed))
        S = lltype.GcStruct('S', ('u', lltype.Ptr(U)))
        A = lltype.GcArray(lltype.Ptr(S))

        def fn():
            s = lltype.malloc(S)
            s.u = lltype.malloc(U)
            a = lltype.malloc(A, 1000)
            gcref1 = lltype.cast_opaque_ptr(llmemory.GCREF, s)
            int1 = rgc.get_rpy_memory_usage(gcref1)
            assert 8 <= int1 <= 32
            gcref2 = lltype.cast_opaque_ptr(llmemory.GCREF, s.u)
            int2 = rgc.get_rpy_memory_usage(gcref2)
            assert 4*9 <= int2 <= 8*12
            gcref3 = lltype.cast_opaque_ptr(llmemory.GCREF, a)
            int3 = rgc.get_rpy_memory_usage(gcref3)
            assert 4*1001 <= int3 <= 8*1010
            return 0

        return fn

    def test_get_rpy_memory_usage(self):
        self.run("get_rpy_memory_usage")

    def define_get_rpy_type_index(self):
        U = lltype.GcStruct('U', ('x', lltype.Signed))
        S = lltype.GcStruct('S', ('u', lltype.Ptr(U)))
        A = lltype.GcArray(lltype.Ptr(S))

        def fn():
            s = lltype.malloc(S)
            s.u = lltype.malloc(U)
            a = lltype.malloc(A, 1000)
            s2 = lltype.malloc(S)
            gcref1 = lltype.cast_opaque_ptr(llmemory.GCREF, s)
            int1 = rgc.get_rpy_type_index(gcref1)
            gcref2 = lltype.cast_opaque_ptr(llmemory.GCREF, s.u)
            int2 = rgc.get_rpy_type_index(gcref2)
            gcref3 = lltype.cast_opaque_ptr(llmemory.GCREF, a)
            int3 = rgc.get_rpy_type_index(gcref3)
            gcref4 = lltype.cast_opaque_ptr(llmemory.GCREF, s2)
            int4 = rgc.get_rpy_type_index(gcref4)
            assert int1 != int2
            assert int1 != int3
            assert int2 != int3
            assert int1 == int4
            return 0

        return fn

    def test_get_rpy_type_index(self):
        self.run("get_rpy_type_index")

    filename1_dump = str(udir.join('test_dump_rpy_heap.1'))
    filename2_dump = str(udir.join('test_dump_rpy_heap.2'))
    def define_dump_rpy_heap(self):
        U = lltype.GcForwardReference()
        U.become(lltype.GcStruct('U', ('next', lltype.Ptr(U)),
                                 ('x', lltype.Signed)))
        S = lltype.GcStruct('S', ('u', lltype.Ptr(U)))
        A = lltype.GcArray(lltype.Ptr(S))
        filename1 = self.filename1_dump
        filename2 = self.filename2_dump

        def fn():
            s = lltype.malloc(S)
            s.u = lltype.malloc(U)
            s.u.next = lltype.malloc(U)
            s.u.next.next = lltype.malloc(U)
            a = lltype.malloc(A, 1000)
            s2 = lltype.malloc(S)
            #
            fd1 = os.open(filename1, os.O_WRONLY | os.O_CREAT, 0666)
            fd2 = os.open(filename2, os.O_WRONLY | os.O_CREAT, 0666)
            rgc.dump_rpy_heap(fd1)
            rgc.dump_rpy_heap(fd2)      # try twice in a row
            keepalive_until_here(s2)
            keepalive_until_here(s)
            keepalive_until_here(a)
            os.close(fd1)
            os.close(fd2)
            return 0

        return fn

    def test_dump_rpy_heap(self):
        self.run("dump_rpy_heap")
        for fn in [self.filename1_dump, self.filename2_dump]:
            assert os.path.exists(fn)
            assert os.path.getsize(fn) > 64
        f = open(self.filename1_dump)
        data1 = f.read()
        f.close()
        f = open(self.filename2_dump)
        data2 = f.read()
        f.close()
        assert data1 == data2

    filename_dump_typeids_z = str(udir.join('test_typeids_z'))
    def define_write_typeids_z(self):
        U = lltype.GcForwardReference()
        U.become(lltype.GcStruct('U', ('next', lltype.Ptr(U)),
                                 ('x', lltype.Signed)))
        S = lltype.GcStruct('S', ('u', lltype.Ptr(U)))
        A = lltype.GcArray(lltype.Ptr(S))
        filename = self.filename_dump_typeids_z
        open_flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_BINARY', 0)

        def fn():
            s = lltype.malloc(S)
            s.u = lltype.malloc(U)
            s.u.next = lltype.malloc(U)
            s.u.next.next = lltype.malloc(U)
            a = lltype.malloc(A, 1000)
            s2 = lltype.malloc(S)
            #
            p = rgc.get_typeids_z()
            s = ''.join([p[i] for i in range(len(p))])
            fd = os.open(filename, open_flags, 0666)
            os.write(fd, s)
            os.close(fd)
            return 0

        return fn

    def test_write_typeids_z(self):
        self.run("write_typeids_z")
        f = open(self.filename_dump_typeids_z, 'rb')
        data_z = f.read()
        f.close()
        import zlib
        data = zlib.decompress(data_z)
        assert data.startswith('member0')
        assert 'GcArray of * GcStruct S {' in data

class TestSemiSpaceGC(TestUsingFramework, snippet.SemiSpaceGCTestDefines):
    gcpolicy = "semispace"
    should_be_moving = True
    GC_CAN_MOVE = True
    GC_CAN_MALLOC_NONMOVABLE = False
    GC_CAN_SHRINK_ARRAY = True

    # for snippets
    large_tests_ok = True

    def define_many_ids(cls):
        from pypy.rlib.objectmodel import compute_unique_id
        class A(object):
            pass
        def f():
            from pypy.rpython.lltypesystem import lltype, rffi
            alist = [A() for i in range(50000)]
            idarray = lltype.malloc(rffi.LONGP.TO, len(alist), flavor='raw')
            # Compute the id of all elements of the list.  The goal is
            # to not allocate memory, so that if the GC needs memory to
            # remember the ids, it will trigger some collections itself
            i = 0
            while i < len(alist):
                idarray[i] = compute_unique_id(alist[i])
                i += 1
            j = 0
            while j < 2:
                if j == 1:     # allocate some stuff between the two iterations
                    [A() for i in range(20000)]
                i = 0
                while i < len(alist):
                    if idarray[i] != compute_unique_id(alist[i]):
                        return j * 1000000 + i
                    i += 1
                j += 1
            lltype.free(idarray, flavor='raw')
            return -2
        return f

    def test_many_ids(self):
        res = self.run('many_ids')
        assert res == -2

    def define_gc_set_max_heap_size(cls):
        def g(n):
            return 'x' * n
        def fn():
            # the semispace size starts at 8MB for now, so setting a
            # smaller limit has no effect
            # set to more than 32MB -- which should be rounded down to 32MB
            rgc.set_max_heap_size(32*1024*1024 + 20000)
            s1 = s2 = s3 = None
            try:
                s1 = g(400000)      # ~ 400 KB
                s2 = g(4000000)     # ~ 4 MB
                s3 = g(40000000)    # ~ 40 MB
            except MemoryError:
                pass
            return (s1 is not None) + (s2 is not None) + (s3 is not None)
        return fn

    def test_gc_set_max_heap_size(self):
        res = self.run('gc_set_max_heap_size')
        assert res == 2

    def define_gc_heap_stats(cls):
        S = lltype.GcStruct('S', ('x', lltype.Signed))
        l1 = []
        l2 = []
        l3 = []
        
        def f():
            for i in range(10):
                s = lltype.malloc(S)
                l1.append(s)
                l2.append(s)
                l3.append(s)
            tb = rgc._heap_stats()
            a = 0
            nr = 0
            b = 0
            c = 0
            for i in range(len(tb)):
                if tb[i].count == 10:      # the type of S
                    a += 1
                    nr = i
            for i in range(len(tb)):
                if tb[i].count == 3:       # the type GcArray(Ptr(S))
                    b += 1
                    c += tb[i].links[nr]
            # b can be 1 or 2 here since _heap_stats() is free to return or
            # ignore the three GcStructs that point to the GcArray(Ptr(S)).
            # important one is c, a is for check
            return c * 100 + b * 10 + a
        return f

    def test_gc_heap_stats(self):
        res = self.run("gc_heap_stats")
        assert res == 3011 or res == 3021

    def definestr_string_builder(cls):
        def fn(_):
            s = StringBuilder()
            s.append("a")
            s.append("abc")
            s.append_slice("abc", 1, 2)
            s.append_multiple_char('d', 4)
            return s.build()
        return fn

    def test_string_builder(self):
        res = self.run('string_builder')
        assert res == "aabcbdddd"
    
    def definestr_string_builder_over_allocation(cls):
        import gc
        def fn(_):
            s = StringBuilder(4)
            s.append("abcd")
            s.append("defg")
            s.append("rty")
            s.append_multiple_char('y', 1000)
            gc.collect()
            s.append_multiple_char('y', 1000)
            res = s.build()
            gc.collect()
            return res
        return fn

    def test_string_builder_over_allocation(self):
        res = self.run('string_builder_over_allocation')
        assert res[1000] == 'y'

    def define_nursery_hash_base(cls):
        from pypy.rlib.objectmodel import compute_identity_hash
        class A:
            pass
        def fn():
            objects = []
            hashes = []
            for i in range(200):
                rgc.collect(0)     # nursery-only collection, if possible
                obj = A()
                objects.append(obj)
                hashes.append(compute_identity_hash(obj))
            unique = {}
            for i in range(len(objects)):
                assert compute_identity_hash(objects[i]) == hashes[i]
                unique[hashes[i]] = None
            return len(unique)
        return fn

    def test_nursery_hash_base(self):
        res = self.run('nursery_hash_base')
        assert res >= 195


class TestGenerationalGC(TestSemiSpaceGC):
    gcpolicy = "generation"
    should_be_moving = True

class TestHybridGC(TestGenerationalGC):
    gcpolicy = "hybrid"
    should_be_moving = True
    GC_CAN_MALLOC_NONMOVABLE = True

    def test_gc_set_max_heap_size(self):
        py.test.skip("not implemented")



class TestHybridGCRemoveTypePtr(TestHybridGC):
    removetypeptr = True


class TestMarkCompactGC(TestSemiSpaceGC):
    gcpolicy = "markcompact"
    should_be_moving = True
    GC_CAN_SHRINK_ARRAY = False

    def test_gc_set_max_heap_size(self):
        py.test.skip("not implemented")

    def test_gc_heap_stats(self):
        py.test.skip("not implemented")

    def test_finalizer_order(self):
        py.test.skip("not implemented")

    def define_adding_a_hash(cls):
        from pypy.rlib.objectmodel import compute_identity_hash
        S1 = lltype.GcStruct('S1', ('x', lltype.Signed))
        S2 = lltype.GcStruct('S2', ('p1', lltype.Ptr(S1)),
                                   ('p2', lltype.Ptr(S1)),
                                   ('p3', lltype.Ptr(S1)),
                                   ('p4', lltype.Ptr(S1)),
                                   ('p5', lltype.Ptr(S1)),
                                   ('p6', lltype.Ptr(S1)),
                                   ('p7', lltype.Ptr(S1)),
                                   ('p8', lltype.Ptr(S1)),
                                   ('p9', lltype.Ptr(S1)))
        def g():
            lltype.malloc(S1)   # forgotten, will be shifted over
            s2 = lltype.malloc(S2)   # a big object, overlaps its old position
            s2.p1 = lltype.malloc(S1); s2.p1.x = 1010
            s2.p2 = lltype.malloc(S1); s2.p2.x = 1020
            s2.p3 = lltype.malloc(S1); s2.p3.x = 1030
            s2.p4 = lltype.malloc(S1); s2.p4.x = 1040
            s2.p5 = lltype.malloc(S1); s2.p5.x = 1050
            s2.p6 = lltype.malloc(S1); s2.p6.x = 1060
            s2.p7 = lltype.malloc(S1); s2.p7.x = 1070
            s2.p8 = lltype.malloc(S1); s2.p8.x = 1080
            s2.p9 = lltype.malloc(S1); s2.p9.x = 1090
            return s2
        def f():
            rgc.collect()
            s2 = g()
            h2 = compute_identity_hash(s2)
            rgc.collect()    # shift s2 to the left, but add a hash field
            assert s2.p1.x == 1010
            assert s2.p2.x == 1020
            assert s2.p3.x == 1030
            assert s2.p4.x == 1040
            assert s2.p5.x == 1050
            assert s2.p6.x == 1060
            assert s2.p7.x == 1070
            assert s2.p8.x == 1080
            assert s2.p9.x == 1090
            return h2 - compute_identity_hash(s2)
        return f

    def test_adding_a_hash(self):
        res = self.run("adding_a_hash")
        assert res == 0

class TestMiniMarkGC(TestSemiSpaceGC):
    gcpolicy = "minimark"
    should_be_moving = True
    GC_CAN_MALLOC_NONMOVABLE = True
    GC_CAN_SHRINK_ARRAY = True

    def test_gc_heap_stats(self):
        py.test.skip("not implemented")

    def define_nongc_attached_to_gc(cls):
        from pypy.rpython.lltypesystem import rffi
        ARRAY = rffi.CArray(rffi.INT)
        class A:
            def __init__(self, n):
                self.buf = lltype.malloc(ARRAY, n, flavor='raw',
                                         add_memory_pressure=True)
            def __del__(self):
                lltype.free(self.buf, flavor='raw')
        A(6)
        def f():
            # allocate a total of ~77GB, but if the automatic gc'ing works,
            # it should never need more than a few MBs at once
            am1 = am2 = am3 = None
            res = 0
            for i in range(1, 100001):
                if am3 is not None:
                    res += rffi.cast(lltype.Signed, am3.buf[0])
                am3 = am2
                am2 = am1
                am1 = A(i * 4)
                am1.buf[0] = rffi.cast(rffi.INT, i-50000)
            return res
        return f

    def test_nongc_attached_to_gc(self):
        res = self.run("nongc_attached_to_gc")
        assert res == -99997

# ____________________________________________________________________

class TaggedPointersTest(object):
    taggedpointers = True

    def define_tagged(cls):
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
        return func

    def test_tagged(self):
        expected = self.run_orig("tagged")
        res = self.run("tagged")
        assert res == expected

    def define_erased(cls):
        from pypy.rlib import rerased
        erase, unerase = rerased.new_erasing_pair("test")
        class Unrelated(object):
            pass

        u = Unrelated()
        u.tagged = True
        u.x = rerased.erase_int(41)
        class A(object):
            pass
        def fn():
            n = 1
            while n >= 0:
                if u.tagged:
                    n = rerased.unerase_int(u.x)
                    a = A()
                    a.n = n - 1
                    u.x = erase(a)
                    u.tagged = False
                else:
                    n = unerase(u.x).n
                    u.x = rerased.erase_int(n - 1)
                    u.tagged = True
        def func():
            rgc.collect() # check that a prebuilt erased integer doesn't explode
            u.x = rerased.erase_int(1000)
            u.tagged = True
            fn()
            return 1
        return func

    def test_erased(self):
        expected = self.run_orig("erased")
        res = self.run("erased")
        assert res == expected

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


class TestHybridTaggedPointers(TaggedPointersTest, TestHybridGC):
    pass

class TestMarkCompactGCMostCompact(TaggedPointersTest, TestMarkCompactGC):
    removetypeptr = True

class TestMiniMarkGCMostCompact(TaggedPointersTest, TestMiniMarkGC):
    removetypeptr = True
