from pypy.module.micronumpy.test.test_base import BaseNumpyAppTest
from pypy.conftest import gettestobjspace


class AppTestNumArray(BaseNumpyAppTest):
    def test_type(self):
        from numpy import array
        ar = array(range(5))
        assert type(ar) is type(ar + ar)

    def test_init(self):
        from numpy import zeros
        a = zeros(15)
        # Check that storage was actually zero'd.
        assert a[10] == 0.0
        # And check that changes stick.
        a[13] = 5.3
        assert a[13] == 5.3

    def test_empty(self):
        """
        Test that empty() works.
        """

        from numpy import empty
        a = empty(2)
        a[1] = 1.0
        assert a[1] == 1.0

    def test_ones(self):
        from numpy import ones
        a = ones(3)
        assert len(a) == 3
        assert a[0] == 1
        raises(IndexError, "a[3]")
        a[2] = 4
        assert a[2] == 4

    def test_copy(self):
        from numpy import array
        a = array(range(5))
        b = a.copy()
        for i in xrange(5):
            assert b[i] == a[i]

    def test_iterator_init(self):
        from numpy import array
        a = array(range(5))
        assert a[3] == 3

    def test_repr(self):
        from numpy import array, zeros
        a = array(range(5), float)
        assert repr(a) == "array([0.0, 1.0, 2.0, 3.0, 4.0])"
        a = array([], float)
        assert repr(a) == "array([], dtype=float64)"
        a = zeros(1001)
        assert repr(a) == "array([0.0, 0.0, 0.0, ..., 0.0, 0.0, 0.0])"
        a = array(range(5), long)
        assert repr(a) == "array([0, 1, 2, 3, 4])"
        a = array([], long)
        assert repr(a) == "array([], dtype=int64)"
        a = array([True, False, True, False], "?")
        assert repr(a) == "array([True, False, True, False], dtype=bool)"

    def test_repr_slice(self):
        from numpy import array, zeros
        a = array(range(5), float)
        b = a[1::2]
        assert repr(b) == "array([1.0, 3.0])"
        a = zeros(2002)
        b = a[::2]
        assert repr(b) == "array([0.0, 0.0, 0.0, ..., 0.0, 0.0, 0.0])"

    def test_str(self):
        from numpy import array, zeros
        a = array(range(5), float)
        assert str(a) == "[0.0 1.0 2.0 3.0 4.0]"
        assert str((2*a)[:]) == "[0.0 2.0 4.0 6.0 8.0]"
        a = zeros(1001)
        assert str(a) == "[0.0 0.0 0.0 ..., 0.0 0.0 0.0]"

        a = array(range(5), dtype=long)
        assert str(a) == "[0 1 2 3 4]"
        a = array([True, False, True, False], dtype="?")
        assert str(a) == "[True False True False]"

        a = array(range(5), dtype="int8")
        assert str(a) == "[0 1 2 3 4]"

        a = array(range(5), dtype="int16")
        assert str(a) == "[0 1 2 3 4]"

    def test_str_slice(self):
        from numpy import array, zeros
        a = array(range(5), float)
        b = a[1::2]
        assert str(b) == "[1.0 3.0]"
        a = zeros(2002)
        b = a[::2]
        assert str(b) == "[0.0 0.0 0.0 ..., 0.0 0.0 0.0]"

    def test_getitem(self):
        from numpy import array
        a = array(range(5))
        raises(IndexError, "a[5]")
        a = a + a
        raises(IndexError, "a[5]")
        assert a[-1] == 8
        raises(IndexError, "a[-6]")

    def test_getitem_tuple(self):
        from numpy import array
        a = array(range(5))
        raises(IndexError, "a[(1,2)]")
        for i in xrange(5):
            assert a[(i,)] == i
        b = a[()]
        for i in xrange(5):
            assert a[i] == b[i]

    def test_setitem(self):
        from numpy import array
        a = array(range(5))
        a[-1] = 5.0
        assert a[4] == 5.0
        raises(IndexError, "a[5] = 0.0")
        raises(IndexError, "a[-6] = 3.0")

    def test_setitem_tuple(self):
        from numpy import array
        a = array(range(5))
        raises(IndexError, "a[(1,2)] = [0,1]")
        for i in xrange(5):
            a[(i,)] = i+1
            assert a[i] == i+1
        a[()] = range(5)
        for i in xrange(5):
            assert a[i] == i

    def test_setslice_array(self):
        from numpy import array
        a = array(range(5))
        b = array(range(2))
        a[1:4:2] = b
        assert a[1] == 0.
        assert a[3] == 1.
        b[::-1] = b
        assert b[0] == 1.
        assert b[1] == 0.

    def test_setslice_of_slice_array(self):
        from numpy import array, zeros
        a = zeros(5)
        a[::2] = array([9., 10., 11.])
        assert a[0] == 9.
        assert a[2] == 10.
        assert a[4] == 11.
        a[1:4:2][::-1] = array([1., 2.])
        assert a[0] == 9.
        assert a[1] == 2.
        assert a[2] == 10.
        assert a[3] == 1.
        assert a[4] == 11.
        a = zeros(10)
        a[::2][::-1][::2] = array(range(1,4))
        assert a[8] == 1.
        assert a[4] == 2.
        assert a[0] == 3.

    def test_setslice_list(self):
        from numpy import array
        a = array(range(5), float)
        b = [0., 1.]
        a[1:4:2] = b
        assert a[1] == 0.
        assert a[3] == 1.

    def test_setslice_constant(self):
        from numpy import array
        a = array(range(5), float)
        a[1:4:2] = 0.
        assert a[1] == 0.
        assert a[3] == 0.

    def test_len(self):
        from numpy import array
        a = array(range(5))
        assert len(a) == 5
        assert len(a + a) == 5

    def test_shape(self):
        from numpy import array
        a = array(range(5))
        assert a.shape == (5,)
        b = a + a
        assert b.shape == (5,)
        c = a[:3]
        assert c.shape == (3,)

    def test_add(self):
        from numpy import array
        a = array(range(5))
        b = a + a
        for i in range(5):
            assert b[i] == i + i

        a = array([True, False, True, False], dtype="?")
        b = array([True, True, False, False], dtype="?")
        c = a + b
        for i in range(4):
            assert c[i] == bool(a[i] + b[i])

    def test_add_other(self):
        from numpy import array
        a = array(range(5))
        b = array(reversed(range(5)))
        c = a + b
        for i in range(5):
            assert c[i] == 4

    def test_add_constant(self):
        from numpy import array
        a = array(range(5))
        b = a + 5
        for i in range(5):
            assert b[i] == i + 5

    def test_radd(self):
        from numpy import array
        r = 3 + array(range(3))
        for i in range(3):
            assert r[i] == i + 3

    def test_add_list(self):
        from numpy import array
        a = array(range(5))
        b = list(reversed(range(5)))
        c = a + b
        assert isinstance(c, array)
        for i in range(5):
            assert c[i] == 4

    def test_subtract(self):
        from numpy import array
        a = array(range(5))
        b = a - a
        for i in range(5):
            assert b[i] == 0

    def test_subtract_other(self):
        from numpy import array
        a = array(range(5))
        b = array([1, 1, 1, 1, 1])
        c = a - b
        for i in range(5):
            assert c[i] == i - 1

    def test_subtract_constant(self):
        from numpy import array
        a = array(range(5))
        b = a - 5
        for i in range(5):
            assert b[i] == i - 5

    def test_mul(self):
        from numpy import array, dtype
        a = array(range(5))
        b = a * a
        for i in range(5):
            assert b[i] == i * i

        a = array(range(5), dtype=bool)
        b = a * a
        assert b.dtype is dtype(bool)
        assert b[0] is False
        for i in range(1, 5):
            assert b[i] is True

    def test_mul_constant(self):
        from numpy import array
        a = array(range(5))
        b = a * 5
        for i in range(5):
            assert b[i] == i * 5

    def test_div(self):
        from math import isnan
        from numpy import array, dtype, inf

        a = array(range(1, 6))
        b = a / a
        for i in range(5):
            assert b[i] == 1

        a = array(range(1, 6), dtype=bool)
        b = a / a
        assert b.dtype is dtype("int8")
        for i in range(5):
            assert b[i] == 1

        a = array([-1, 0, 1])
        b = array([0, 0, 0])
        c = a / b
        assert (c == [0, 0, 0]).all()

        a = array([-1.0, 0.0, 1.0])
        b = array([0.0, 0.0, 0.0])
        c = a / b
        assert c[0] == -inf
        assert isnan(c[1])
        assert c[2] == inf

        b = array([-0.0, -0.0, -0.0])
        c = a / b
        assert c[0] == inf
        assert isnan(c[1])
        assert c[2] == -inf

    def test_div_other(self):
        from numpy import array
        a = array(range(5))
        b = array([2, 2, 2, 2, 2], float)
        c = a / b
        for i in range(5):
            assert c[i] == i / 2.0

    def test_div_constant(self):
        from numpy import array
        a = array(range(5))
        b = a / 5.0
        for i in range(5):
            assert b[i] == i / 5.0

    def test_pow(self):
        from numpy import array
        a = array(range(5), float)
        b = a ** a
        for i in range(5):
            print b[i], i**i
            assert b[i] == i**i

    def test_pow_other(self):
        from numpy import array
        a = array(range(5), float)
        b = array([2, 2, 2, 2, 2])
        c = a ** b
        for i in range(5):
            assert c[i] == i ** 2

    def test_pow_constant(self):
        from numpy import array
        a = array(range(5), float)
        b = a ** 2
        for i in range(5):
            assert b[i] == i ** 2

    def test_mod(self):
        from numpy import array
        a = array(range(1,6))
        b = a % a
        for i in range(5):
            assert b[i] == 0

        a = array(range(1, 6), float)
        b = (a + 1) % a
        assert b[0] == 0
        for i in range(1, 5):
            assert b[i] == 1

    def test_mod_other(self):
        from numpy import array
        a = array(range(5))
        b = array([2, 2, 2, 2, 2])
        c = a % b
        for i in range(5):
            assert c[i] == i % 2

    def test_mod_constant(self):
        from numpy import array
        a = array(range(5))
        b = a % 2
        for i in range(5):
            assert b[i] == i % 2

    def test_pos(self):
        from numpy import array
        a = array([1.,-2.,3.,-4.,-5.])
        b = +a
        for i in range(5):
            assert b[i] == a[i]

        a = +array(range(5))
        for i in range(5):
            assert a[i] == i

    def test_neg(self):
        from numpy import array
        a = array([1.,-2.,3.,-4.,-5.])
        b = -a
        for i in range(5):
            assert b[i] == -a[i]

        a = -array(range(5), dtype="int8")
        for i in range(5):
            assert a[i] == -i

    def test_abs(self):
        from numpy import array
        a = array([1.,-2.,3.,-4.,-5.])
        b = abs(a)
        for i in range(5):
            assert b[i] == abs(a[i])

        a = abs(array(range(-5, 5), dtype="int8"))
        for i in range(-5, 5):
            assert a[i + 5] == abs(i)

    def test_auto_force(self):
        from numpy import array
        a = array(range(5))
        b = a - 1
        a[2] = 3
        for i in range(5):
            assert b[i] == i - 1

        a = array(range(5))
        b = a + a
        c = b + b
        b[1] = 5
        assert c[1] == 4

    def test_getslice(self):
        from numpy import array
        a = array(range(5))
        s = a[1:5]
        assert len(s) == 4
        for i in range(4):
            assert s[i] == a[i+1]

        s = (a + a)[1:2]
        assert len(s) == 1
        assert s[0] == 2
        s[:1] = array([5])
        assert s[0] == 5

    def test_getslice_step(self):
        from numpy import array
        a = array(range(10))
        s = a[1:9:2]
        assert len(s) == 4
        for i in range(4):
            assert s[i] == a[2*i+1]

    def test_slice_update(self):
        from numpy import array
        a = array(range(5))
        s = a[0:3]
        s[1] = 10
        assert a[1] == 10
        a[2] = 20
        assert s[2] == 20


    def test_slice_invaidate(self):
        # check that slice shares invalidation list with
        from numpy import array
        a = array(range(5))
        s = a[0:2]
        b = array([10,11])
        c = s + b
        a[0] = 100
        assert c[0] == 10
        assert c[1] == 12
        d = s + b
        a[1] = 101
        assert d[0] == 110
        assert d[1] == 12

    def test_mean(self):
        from numpy import array
        a = array(range(5))
        assert a.mean() == 2.0
        assert a[:4].mean() == 1.5

    def test_sum(self):
        from numpy import array
        a = array(range(5))
        assert a.sum() == 10.0
        assert a[:4].sum() == 6.0

        a = array([True] * 5, bool)
        assert a.sum() == 5

    def test_prod(self):
        from numpy import array
        a = array(range(1,6))
        assert a.prod() == 120.0
        assert a[:4].prod() == 24.0

    def test_max(self):
        from numpy import array
        a = array([-1.2, 3.4, 5.7, -3.0, 2.7])
        assert a.max() == 5.7
        b = array([])
        raises(ValueError, "b.max()")

    def test_max_add(self):
        from numpy import array
        a = array([-1.2, 3.4, 5.7, -3.0, 2.7])
        assert (a+a).max() == 11.4

    def test_min(self):
        from numpy import array
        a = array([-1.2, 3.4, 5.7, -3.0, 2.7])
        assert a.min() == -3.0
        b = array([])
        raises(ValueError, "b.min()")

    def test_argmax(self):
        from numpy import array
        a = array([-1.2, 3.4, 5.7, -3.0, 2.7])
        assert a.argmax() == 2
        b = array([])
        raises(ValueError, "b.argmax()")

        a = array(range(-5, 5))
        assert a.argmax() == 9

    def test_argmin(self):
        from numpy import array
        a = array([-1.2, 3.4, 5.7, -3.0, 2.7])
        assert a.argmin() == 3
        b = array([])
        raises(ValueError, "b.argmin()")

    def test_all(self):
        from numpy import array
        a = array(range(5))
        assert a.all() == False
        a[0] = 3.0
        assert a.all() == True
        b = array([])
        assert b.all() == True

    def test_any(self):
        from numpy import array, zeros
        a = array(range(5))
        assert a.any() == True
        b = zeros(5)
        assert b.any() == False
        c = array([])
        assert c.any() == False

    def test_dot(self):
        from numpy import array
        a = array(range(5))
        assert a.dot(a) == 30.0

        a = array(range(5))
        assert a.dot(range(5)) == 30

    def test_dot_constant(self):
        from numpy import array
        a = array(range(5))
        b = a.dot(2.5)
        for i in xrange(5):
            assert b[i] == 2.5 * a[i]

    def test_dtype_guessing(self):
        from numpy import array, dtype

        assert array([True]).dtype is dtype(bool)
        assert array([True, False]).dtype is dtype(bool)
        assert array([True, 1]).dtype is dtype(int)
        assert array([1, 2, 3]).dtype is dtype(int)
        assert array([1L, 2, 3]).dtype is dtype(long)
        assert array([1.2, True]).dtype is dtype(float)
        assert array([1.2, 5]).dtype is dtype(float)
        assert array([]).dtype is dtype(float)

    def test_comparison(self):
        import operator
        from numpy import array, dtype

        a = array(range(5))
        b = array(range(5), float)
        for func in [
            operator.eq, operator.ne, operator.lt, operator.le, operator.gt,
            operator.ge
        ]:
            c = func(a, 3)
            assert c.dtype is dtype(bool)
            for i in xrange(5):
                assert c[i] == func(a[i], 3)

            c = func(b, 3)
            assert c.dtype is dtype(bool)
            for i in xrange(5):
                assert c[i] == func(b[i], 3)


class AppTestSupport(object):
    def setup_class(cls):
        import struct
        cls.space = gettestobjspace(usemodules=('micronumpy',))
        cls.w_data = cls.space.wrap(struct.pack('dddd', 1, 2, 3, 4))

    def test_fromstring(self):
        from numpy import fromstring
        a = fromstring(self.data)
        for i in range(4):
            assert a[i] == i + 1
        raises(ValueError, fromstring, "abc")
