"""
Implementation of a part of the standard Python opcodes.
The rest, dealing with variables in optimized ways, is in
pyfastscope.py and pynestedscope.py.
"""

from pypy.interpreter.error import OperationError
from pypy.interpreter.baseobjspace import UnpackValueError, Wrappable
from pypy.interpreter import gateway, function, eval
from pypy.interpreter import pyframe, pytraceback
from pypy.interpreter.argument import Arguments, ArgumentsFromValuestack
from pypy.interpreter.pycode import PyCode
from pypy.tool.sourcetools import func_with_new_name
from pypy.rlib.objectmodel import we_are_translated, hint
from pypy.rlib.rarithmetic import r_uint, intmask
from pypy.tool.stdlib_opcode import opcodedesc, HAVE_ARGUMENT
from pypy.tool.stdlib_opcode import unrolling_opcode_descs
from pypy.tool.stdlib_opcode import opcode_method_names
from pypy.rlib import rstack # for resume points

def unaryoperation(operationname):
    """NOT_RPYTHON"""
    def opimpl(f, *ignored):
        operation = getattr(f.space, operationname)
        w_1 = f.valuestack.pop()
        w_result = operation(w_1)
        f.valuestack.push(w_result)

    return func_with_new_name(opimpl, "opcode_impl_for_%s" % operationname)

def binaryoperation(operationname):
    """NOT_RPYTHON"""    
    def opimpl(f, *ignored):
        operation = getattr(f.space, operationname)
        w_2 = f.valuestack.pop()
        w_1 = f.valuestack.pop()
        w_result = operation(w_1, w_2)
        f.valuestack.push(w_result)

    return func_with_new_name(opimpl, "opcode_impl_for_%s" % operationname)        


class __extend__(pyframe.PyFrame):
    """A PyFrame that knows about interpretation of standard Python opcodes
    minus the ones related to nested scopes."""
    
    ### opcode dispatch ###

    def dispatch(self, co_code, next_instr, ec):
        while True:
            try:
                return self.dispatch_bytecode(co_code, next_instr, ec)
            except OperationError, operr:
                next_instr = self.handle_operation_error(ec, operr)
            except Reraise:
                operr = self.last_exception
                next_instr = self.handle_operation_error(ec, operr,
                                                         attach_tb=False)
            except KeyboardInterrupt:
                next_instr = self.handle_asynchronous_error(ec,
                    self.space.w_KeyboardInterrupt)
            except MemoryError:
                next_instr = self.handle_asynchronous_error(ec,
                    self.space.w_MemoryError)
            except RuntimeError:
                if we_are_translated():
                    # stack overflows should be the only kind of RuntimeErrors
                    # in translated PyPy
                    msg = "internal error (stack overflow?)"
                else:
                    msg = str(e)
                next_instr = self.handle_asynchronous_error(ec,
                    self.space.w_RuntimeError,
                    self.space.wrap(msg))

    def handle_asynchronous_error(self, ec, w_type, w_value=None):
        # catch asynchronous exceptions and turn them
        # into OperationErrors
        if w_value is None:
            w_value = self.space.w_None
        operr = OperationError(w_type, w_value)
        return self.handle_operation_error(ec, operr)

    def handle_operation_error(self, ec, operr, attach_tb=True):
        self.last_exception = operr
        if attach_tb:
            pytraceback.record_application_traceback(
                self.space, operr, self, self.last_instr)
            ec.exception_trace(self, operr)

        block = self.unrollstack(SApplicationException.kind)
        if block is None:
            # no handler found for the OperationError
            tb = cpython_tb()
            raise OperationError, operr, tb
        else:
            unroller = SApplicationException(operr)
            next_instr = block.handle(self, unroller)
            return next_instr

    def dispatch_bytecode(self, co_code, next_instr, ec):
        space = self.space
        while True:
            self.last_instr = intmask(next_instr)
            ec.bytecode_trace(self)
            # For the sequel, force 'next_instr' to be unsigned for performance
            next_instr = r_uint(self.last_instr)
            opcode = ord(co_code[next_instr])
            next_instr += 1
            if space.config.objspace.logbytecodes:
                space.bytecodecounts[opcode] = space.bytecodecounts.get(opcode, 0) + 1

            if opcode >= HAVE_ARGUMENT:
                lo = ord(co_code[next_instr])
                hi = ord(co_code[next_instr+1])
                next_instr += 2
                oparg = (hi << 8) | lo
            else:
                oparg = 0

            while opcode == opcodedesc.EXTENDED_ARG.index:
                opcode = ord(co_code[next_instr])
                if opcode < HAVE_ARGUMENT:
                    raise BytecodeCorruption
                lo = ord(co_code[next_instr+1])
                hi = ord(co_code[next_instr+2])
                next_instr += 3
                oparg = (oparg << 16) | (hi << 8) | lo

            if opcode == opcodedesc.RETURN_VALUE.index:
                w_returnvalue = self.valuestack.pop()
                block = self.unrollstack(SReturnValue.kind)
                if block is None:
                    self.frame_finished_execution = True  # for generators
                    return w_returnvalue
                else:
                    unroller = SReturnValue(w_returnvalue)
                    next_instr = block.handle(self, unroller)
                    continue    # now inside a 'finally' block

            if opcode == opcodedesc.YIELD_VALUE.index:
                w_yieldvalue = self.valuestack.pop()
                return w_yieldvalue

            if opcode == opcodedesc.END_FINALLY.index:
                # unlike CPython, when we reach this opcode the value stack has
                # always been set up as follows (topmost first):
                #   [exception type  or None]
                #   [exception value or None]
                #   [wrapped stack unroller ]
                self.valuestack.pop()   # ignore the exception type
                self.valuestack.pop()   # ignore the exception value
                w_unroller = self.valuestack.pop()
                unroller = self.space.interpclass_w(w_unroller)
                if isinstance(unroller, SuspendedUnroller):
                    # go on unrolling the stack
                    block = self.unrollstack(unroller.kind)
                    if block is None:
                        self.frame_finished_execution = True  # for generators
                        return unroller.nomoreblocks()
                    else:
                        next_instr = block.handle(self, unroller)
                continue

            if we_are_translated():
                for opdesc in unrolling_opcode_descs:
                    # static checks to skip this whole case if necessary
                    if not opdesc.is_enabled(space):
                        continue
                    if not hasattr(pyframe.PyFrame, opdesc.methodname):
                        continue   # e.g. for JUMP_FORWARD, implemented above

                    if opcode == opdesc.index:
                        # dispatch to the opcode method
                        meth = getattr(self, opdesc.methodname)
                        res = meth(oparg, next_instr)
                        # !! warning, for the annotator the next line is not
                        # comparing an int and None - you can't do that.
                        # Instead, it's constant-folded to either True or False
                        if res is not None:
                            next_instr = res
                        break
                else:
                    self.MISSING_OPCODE(oparg, next_instr)

            else:  # when we are not translated, a list lookup is much faster
                methodname = opcode_method_names[opcode]
                res = getattr(self, methodname)(oparg, next_instr)
                if res is not None:
                    next_instr = res

    def unrollstack(self, unroller_kind):
        while not self.blockstack.empty():
            block = self.blockstack.pop()
            if (block.handling_mask & unroller_kind) != 0:
                return block
        return None

    def unrollstack_and_jump(self, unroller):
        block = self.unrollstack(unroller.kind)
        if block is None:
            raise BytecodeCorruption("misplaced bytecode - should not return")
        return block.handle(self, unroller)

    ### accessor functions ###

    def getlocalvarname(self, index):
        return self.pycode.co_varnames[index]

    def getconstant_w(self, index):
        return self.pycode.co_consts_w[index]

    def getname_u(self, index):
        return self.space.str_w(self.pycode.co_names_w[index])

    def getname_w(self, index):
        return self.pycode.co_names_w[index]


    ################################################################
    ##  Implementation of the "operational" opcodes
    ##  See also pyfastscope.py and pynestedscope.py for the rest.
    ##
    
    #  the 'self' argument of opcode implementations is called 'f'
    #  for historical reasons

    def NOP(f, *ignored):
        pass

    def LOAD_FAST(f, varindex, *ignored):
        # access a local variable directly
        w_value = f.fastlocals_w[varindex]
        if w_value is None:
            varname = f.getlocalvarname(varindex)
            message = "local variable '%s' referenced before assignment" % varname
            raise OperationError(f.space.w_UnboundLocalError, f.space.wrap(message))
        f.valuestack.push(w_value)

    def LOAD_CONST(f, constindex, *ignored):
        w_const = f.getconstant_w(constindex)
        f.valuestack.push(w_const)

    def STORE_FAST(f, varindex, *ignored):
        w_newvalue = f.valuestack.pop()
        f.fastlocals_w[varindex] = w_newvalue
        #except:
        #    print "exception: got index error"
        #    print " varindex:", varindex
        #    print " len(locals_w)", len(f.locals_w)
        #    import dis
        #    print dis.dis(f.pycode)
        #    print "co_varnames", f.pycode.co_varnames
        #    print "co_nlocals", f.pycode.co_nlocals
        #    raise

    def POP_TOP(f, *ignored):
        f.valuestack.pop()

    def ROT_TWO(f, *ignored):
        w_1 = f.valuestack.pop()
        w_2 = f.valuestack.pop()
        f.valuestack.push(w_1)
        f.valuestack.push(w_2)

    def ROT_THREE(f, *ignored):
        w_1 = f.valuestack.pop()
        w_2 = f.valuestack.pop()
        w_3 = f.valuestack.pop()
        f.valuestack.push(w_1)
        f.valuestack.push(w_3)
        f.valuestack.push(w_2)

    def ROT_FOUR(f, *ignored):
        w_1 = f.valuestack.pop()
        w_2 = f.valuestack.pop()
        w_3 = f.valuestack.pop()
        w_4 = f.valuestack.pop()
        f.valuestack.push(w_1)
        f.valuestack.push(w_4)
        f.valuestack.push(w_3)
        f.valuestack.push(w_2)

    def DUP_TOP(f, *ignored):
        w_1 = f.valuestack.top()
        f.valuestack.push(w_1)

    def DUP_TOPX(f, itemcount, *ignored):
        assert 1 <= itemcount <= 5, "limitation of the current interpreter"
        for i in range(itemcount):
            w_1 = f.valuestack.top(itemcount-1)
            f.valuestack.push(w_1)

    UNARY_POSITIVE = unaryoperation("pos")
    UNARY_NEGATIVE = unaryoperation("neg")
    UNARY_NOT      = unaryoperation("not_")
    UNARY_CONVERT  = unaryoperation("repr")
    UNARY_INVERT   = unaryoperation("invert")

    def BINARY_POWER(f, *ignored):
        w_2 = f.valuestack.pop()
        w_1 = f.valuestack.pop()
        w_result = f.space.pow(w_1, w_2, f.space.w_None)
        f.valuestack.push(w_result)

    BINARY_MULTIPLY = binaryoperation("mul")
    BINARY_TRUE_DIVIDE  = binaryoperation("truediv")
    BINARY_FLOOR_DIVIDE = binaryoperation("floordiv")
    BINARY_DIVIDE       = binaryoperation("div")
    # XXX BINARY_DIVIDE must fall back to BINARY_TRUE_DIVIDE with -Qnew
    BINARY_MODULO       = binaryoperation("mod")
    BINARY_ADD      = binaryoperation("add")
    BINARY_SUBTRACT = binaryoperation("sub")
    BINARY_SUBSCR   = binaryoperation("getitem")
    BINARY_LSHIFT   = binaryoperation("lshift")
    BINARY_RSHIFT   = binaryoperation("rshift")
    BINARY_AND = binaryoperation("and_")
    BINARY_XOR = binaryoperation("xor")
    BINARY_OR  = binaryoperation("or_")

    def INPLACE_POWER(f, *ignored):
        w_2 = f.valuestack.pop()
        w_1 = f.valuestack.pop()
        w_result = f.space.inplace_pow(w_1, w_2)
        f.valuestack.push(w_result)

    INPLACE_MULTIPLY = binaryoperation("inplace_mul")
    INPLACE_TRUE_DIVIDE  = binaryoperation("inplace_truediv")
    INPLACE_FLOOR_DIVIDE = binaryoperation("inplace_floordiv")
    INPLACE_DIVIDE       = binaryoperation("inplace_div")
    # XXX INPLACE_DIVIDE must fall back to INPLACE_TRUE_DIVIDE with -Qnew
    INPLACE_MODULO       = binaryoperation("inplace_mod")
    INPLACE_ADD      = binaryoperation("inplace_add")
    INPLACE_SUBTRACT = binaryoperation("inplace_sub")
    INPLACE_LSHIFT   = binaryoperation("inplace_lshift")
    INPLACE_RSHIFT   = binaryoperation("inplace_rshift")
    INPLACE_AND = binaryoperation("inplace_and")
    INPLACE_XOR = binaryoperation("inplace_xor")
    INPLACE_OR  = binaryoperation("inplace_or")

    def slice(f, w_start, w_end):
        w_obj = f.valuestack.pop()
        w_result = f.space.getslice(w_obj, w_start, w_end)
        f.valuestack.push(w_result)

    def SLICE_0(f, *ignored):
        f.slice(f.space.w_None, f.space.w_None)

    def SLICE_1(f, *ignored):
        w_start = f.valuestack.pop()
        f.slice(w_start, f.space.w_None)

    def SLICE_2(f, *ignored):
        w_end = f.valuestack.pop()
        f.slice(f.space.w_None, w_end)

    def SLICE_3(f, *ignored):
        w_end = f.valuestack.pop()
        w_start = f.valuestack.pop()
        f.slice(w_start, w_end)

    def storeslice(f, w_start, w_end):
        w_obj = f.valuestack.pop()
        w_newvalue = f.valuestack.pop()
        f.space.setslice(w_obj, w_start, w_end, w_newvalue)

    def STORE_SLICE_0(f, *ignored):
        f.storeslice(f.space.w_None, f.space.w_None)

    def STORE_SLICE_1(f, *ignored):
        w_start = f.valuestack.pop()
        f.storeslice(w_start, f.space.w_None)

    def STORE_SLICE_2(f, *ignored):
        w_end = f.valuestack.pop()
        f.storeslice(f.space.w_None, w_end)

    def STORE_SLICE_3(f, *ignored):
        w_end = f.valuestack.pop()
        w_start = f.valuestack.pop()
        f.storeslice(w_start, w_end)

    def deleteslice(f, w_start, w_end):
        w_obj = f.valuestack.pop()
        f.space.delslice(w_obj, w_start, w_end)

    def DELETE_SLICE_0(f, *ignored):
        f.deleteslice(f.space.w_None, f.space.w_None)

    def DELETE_SLICE_1(f, *ignored):
        w_start = f.valuestack.pop()
        f.deleteslice(w_start, f.space.w_None)

    def DELETE_SLICE_2(f, *ignored):
        w_end = f.valuestack.pop()
        f.deleteslice(f.space.w_None, w_end)

    def DELETE_SLICE_3(f, *ignored):
        w_end = f.valuestack.pop()
        w_start = f.valuestack.pop()
        f.deleteslice(w_start, w_end)

    def STORE_SUBSCR(f, *ignored):
        "obj[subscr] = newvalue"
        w_subscr = f.valuestack.pop()
        w_obj = f.valuestack.pop()
        w_newvalue = f.valuestack.pop()
        f.space.setitem(w_obj, w_subscr, w_newvalue)

    def DELETE_SUBSCR(f, *ignored):
        "del obj[subscr]"
        w_subscr = f.valuestack.pop()
        w_obj = f.valuestack.pop()
        f.space.delitem(w_obj, w_subscr)

    def PRINT_EXPR(f, *ignored):
        w_expr = f.valuestack.pop()
        print_expr(f.space, w_expr)

    def PRINT_ITEM_TO(f, *ignored):
        w_stream = f.valuestack.pop()
        w_item = f.valuestack.pop()
        if f.space.is_w(w_stream, f.space.w_None):
            w_stream = sys_stdout(f.space)   # grumble grumble special cases
        print_item_to(f.space, w_item, w_stream)

    def PRINT_ITEM(f, *ignored):
        w_item = f.valuestack.pop()
        print_item(f.space, w_item)

    def PRINT_NEWLINE_TO(f, *ignored):
        w_stream = f.valuestack.pop()
        if f.space.is_w(w_stream, f.space.w_None):
            w_stream = sys_stdout(f.space)   # grumble grumble special cases
        print_newline_to(f.space, w_stream)

    def PRINT_NEWLINE(f, *ignored):
        print_newline(f.space)

    def BREAK_LOOP(f, *ignored):
        next_instr = f.unrollstack_and_jump(SBreakLoop.singleton)
        return next_instr

    def CONTINUE_LOOP(f, startofloop, *ignored):
        unroller = SContinueLoop(startofloop)
        next_instr = f.unrollstack_and_jump(unroller)
        return next_instr

    def RAISE_VARARGS(f, nbargs, *ignored):
        space = f.space
        if nbargs == 0:
            operror = space.getexecutioncontext().sys_exc_info()
            if operror is None:
                raise OperationError(space.w_TypeError,
                    space.wrap("raise: no active exception to re-raise"))
            # re-raise, no new traceback obj will be attached
            f.last_exception = operror
            raise Reraise

        w_value = w_traceback = space.w_None
        if nbargs >= 3: w_traceback = f.valuestack.pop()
        if nbargs >= 2: w_value     = f.valuestack.pop()
        if 1:           w_type      = f.valuestack.pop()
        operror = OperationError(w_type, w_value)
        operror.normalize_exception(space)
        if not space.full_exceptions or space.is_w(w_traceback, space.w_None):
            # common case
            raise operror
        else:
            tb = space.interpclass_w(w_traceback)
            if tb is None or not space.is_true(space.isinstance(tb, 
                space.gettypeobject(pytraceback.PyTraceback.typedef))):
                raise OperationError(space.w_TypeError,
                      space.wrap("raise: arg 3 must be a traceback or None"))
            operror.application_traceback = tb
            # re-raise, no new traceback obj will be attached
            f.last_exception = operror
            raise Reraise

    def LOAD_LOCALS(f, *ignored):
        f.valuestack.push(f.w_locals)

    def EXEC_STMT(f, *ignored):
        w_locals  = f.valuestack.pop()
        w_globals = f.valuestack.pop()
        w_prog    = f.valuestack.pop()
        flags = f.space.getexecutioncontext().compiler.getcodeflags(f.pycode)
        w_compile_flags = f.space.wrap(flags)
        w_resulttuple = prepare_exec(f.space, f.space.wrap(f), w_prog,
                                     w_globals, w_locals,
                                     w_compile_flags, f.space.wrap(f.builtin),
                                     f.space.gettypeobject(PyCode.typedef))
        w_prog, w_globals, w_locals = f.space.unpacktuple(w_resulttuple, 3)

        plain = f.w_locals is not None and f.space.is_w(w_locals, f.w_locals)
        if plain:
            w_locals = f.getdictscope()
        co = f.space.interp_w(eval.Code, w_prog)
        co.exec_code(f.space, w_globals, w_locals)
        if plain:
            f.setdictscope(w_locals)

    def POP_BLOCK(f, *ignored):
        block = f.blockstack.pop()
        block.cleanup(f)  # the block knows how to clean up the value stack

    def BUILD_CLASS(f, *ignored):
        w_methodsdict = f.valuestack.pop()
        w_bases       = f.valuestack.pop()
        w_name        = f.valuestack.pop()
        w_metaclass = find_metaclass(f.space, w_bases,
                                     w_methodsdict, f.w_globals,
                                     f.space.wrap(f.builtin)) 
        w_newclass = f.space.call_function(w_metaclass, w_name,
                                           w_bases, w_methodsdict)
        f.valuestack.push(w_newclass)

    def STORE_NAME(f, varindex, *ignored):
        w_varname = f.getname_w(varindex)
        w_newvalue = f.valuestack.pop()
        f.space.set_str_keyed_item(f.w_locals, w_varname, w_newvalue)

    def DELETE_NAME(f, varindex, *ignored):
        w_varname = f.getname_w(varindex)
        try:
            f.space.delitem(f.w_locals, w_varname)
        except OperationError, e:
            # catch KeyErrors and turn them into NameErrors
            if not e.match(f.space, f.space.w_KeyError):
                raise
            message = "name '%s' is not defined" % f.space.str_w(w_varname)
            raise OperationError(f.space.w_NameError, f.space.wrap(message))

    def UNPACK_SEQUENCE(f, itemcount, *ignored):
        w_iterable = f.valuestack.pop()
        try:
            items = f.space.unpackiterable(w_iterable, itemcount)
        except UnpackValueError, e:
            raise OperationError(f.space.w_ValueError, f.space.wrap(e.msg))
        items.reverse()
        for item in items:
            f.valuestack.push(item)

    def STORE_ATTR(f, nameindex, *ignored):
        "obj.attributename = newvalue"
        w_attributename = f.getname_w(nameindex)
        w_obj = f.valuestack.pop()
        w_newvalue = f.valuestack.pop()
        f.space.setattr(w_obj, w_attributename, w_newvalue)

    def DELETE_ATTR(f, nameindex, *ignored):
        "del obj.attributename"
        w_attributename = f.getname_w(nameindex)
        w_obj = f.valuestack.pop()
        f.space.delattr(w_obj, w_attributename)

    def STORE_GLOBAL(f, nameindex, *ignored):
        w_varname = f.getname_w(nameindex)
        w_newvalue = f.valuestack.pop()
        f.space.set_str_keyed_item(f.w_globals, w_varname, w_newvalue)

    def DELETE_GLOBAL(f, nameindex, *ignored):
        w_varname = f.getname_w(nameindex)
        f.space.delitem(f.w_globals, w_varname)

    def LOAD_NAME(f, nameindex, *ignored):
        if f.w_locals is not f.w_globals:
            w_varname = f.getname_w(nameindex)
            w_value = f.space.finditem(f.w_locals, w_varname)
            if w_value is not None:
                f.valuestack.push(w_value)
                return
        f.LOAD_GLOBAL(nameindex)    # fall-back

    def LOAD_GLOBAL(f, nameindex, *ignored):
        w_varname = f.getname_w(nameindex)
        w_value = f.space.finditem(f.w_globals, w_varname)
        if w_value is None:
            # not in the globals, now look in the built-ins
            w_value = f.builtin.getdictvalue(f.space, w_varname)
            if w_value is None:
                varname = f.getname_u(nameindex)
                message = "global name '%s' is not defined" % varname
                raise OperationError(f.space.w_NameError,
                                     f.space.wrap(message))
        f.valuestack.push(w_value)

    def DELETE_FAST(f, varindex, *ignored):
        if f.fastlocals_w[varindex] is None:
            varname = f.getlocalvarname(varindex)
            message = "local variable '%s' referenced before assignment" % varname
            raise OperationError(f.space.w_UnboundLocalError, f.space.wrap(message))
        f.fastlocals_w[varindex] = None
        

    def BUILD_TUPLE(f, itemcount, *ignored):
        items = [f.valuestack.pop() for i in range(itemcount)]
        items.reverse()
        w_tuple = f.space.newtuple(items)
        f.valuestack.push(w_tuple)

    def BUILD_LIST(f, itemcount, *ignored):
        items = [f.valuestack.pop() for i in range(itemcount)]
        items.reverse()
        w_list = f.space.newlist(items)
        f.valuestack.push(w_list)

    def BUILD_MAP(f, zero, *ignored):
        if zero != 0:
            raise BytecodeCorruption
        w_dict = f.space.newdict()
        f.valuestack.push(w_dict)

    def LOAD_ATTR(f, nameindex, *ignored):
        "obj.attributename"
        w_attributename = f.getname_w(nameindex)
        w_obj = f.valuestack.pop()
        w_value = f.space.getattr(w_obj, w_attributename)
        f.valuestack.push(w_value)

    def cmp_lt(f, w_1, w_2):  return f.space.lt(w_1, w_2)
    def cmp_le(f, w_1, w_2):  return f.space.le(w_1, w_2)
    def cmp_eq(f, w_1, w_2):  return f.space.eq(w_1, w_2)
    def cmp_ne(f, w_1, w_2):  return f.space.ne(w_1, w_2)
    def cmp_gt(f, w_1, w_2):  return f.space.gt(w_1, w_2)
    def cmp_ge(f, w_1, w_2):  return f.space.ge(w_1, w_2)

    def cmp_in(f, w_1, w_2):
        return f.space.contains(w_2, w_1)
    def cmp_not_in(f, w_1, w_2):
        return f.space.not_(f.space.contains(w_2, w_1))
    def cmp_is(f, w_1, w_2):
        return f.space.is_(w_1, w_2)
    def cmp_is_not(f, w_1, w_2):
        return f.space.not_(f.space.is_(w_1, w_2))
    def cmp_exc_match(f, w_1, w_2):
        return f.space.newbool(f.space.exception_match(w_1, w_2))

    compare_dispatch_table = [
        cmp_lt,   # "<"
        cmp_le,   # "<="
        cmp_eq,   # "=="
        cmp_ne,   # "!="
        cmp_gt,   # ">"
        cmp_ge,   # ">="
        cmp_in,
        cmp_not_in,
        cmp_is,
        cmp_is_not,
        cmp_exc_match,
        ]
    def COMPARE_OP(f, testnum, *ignored):
        w_2 = f.valuestack.pop()
        w_1 = f.valuestack.pop()
        try:
            testfn = f.compare_dispatch_table[testnum]
        except IndexError:
            raise BytecodeCorruption, "bad COMPARE_OP oparg"
        w_result = testfn(f, w_1, w_2)
        f.valuestack.push(w_result)

    def IMPORT_NAME(f, nameindex, *ignored):
        space = f.space
        w_modulename = f.getname_w(nameindex)
        modulename = f.space.str_w(w_modulename)
        w_fromlist = f.valuestack.pop()
        w_import = f.builtin.getdictvalue_w(f.space, '__import__')
        if w_import is None:
            raise OperationError(space.w_ImportError,
                                 space.wrap("__import__ not found"))
        w_locals = f.w_locals
        if w_locals is None:            # CPython does this
            w_locals = space.w_None
        w_obj = space.call_function(w_import, space.wrap(modulename),
                                    f.w_globals, w_locals, w_fromlist)
        f.valuestack.push(w_obj)

    def IMPORT_STAR(f, *ignored):
        w_module = f.valuestack.pop()
        w_locals = f.getdictscope()
        import_all_from(f.space, w_module, w_locals)
        f.setdictscope(w_locals)

    def IMPORT_FROM(f, nameindex, *ignored):
        w_name = f.getname_w(nameindex)
        w_module = f.valuestack.top()
        try:
            w_obj = f.space.getattr(w_module, w_name)
        except OperationError, e:
            if not e.match(f.space, f.space.w_AttributeError):
                raise
            raise OperationError(f.space.w_ImportError,
                             f.space.wrap("cannot import name '%s'" % f.space.str_w(w_name) ))
        f.valuestack.push(w_obj)

    def JUMP_FORWARD(f, jumpby, next_instr, *ignored):
        next_instr += jumpby
        return next_instr

    def JUMP_IF_FALSE(f, stepby, next_instr, *ignored):
        w_cond = f.valuestack.top()
        if not f.space.is_true(w_cond):
            next_instr += stepby
        return next_instr

    def JUMP_IF_TRUE(f, stepby, next_instr, *ignored):
        w_cond = f.valuestack.top()
        if f.space.is_true(w_cond):
            next_instr += stepby
        return next_instr

    def JUMP_ABSOLUTE(f, jumpto, next_instr, *ignored):
        return jumpto

    def GET_ITER(f, *ignored):
        w_iterable = f.valuestack.pop()
        w_iterator = f.space.iter(w_iterable)
        f.valuestack.push(w_iterator)

    def FOR_ITER(f, jumpby, next_instr, *ignored):
        w_iterator = f.valuestack.top()
        try:
            w_nextitem = f.space.next(w_iterator)
        except OperationError, e:
            if not e.match(f.space, f.space.w_StopIteration):
                raise 
            # iterator exhausted
            f.valuestack.pop()
            next_instr += jumpby
        else:
            f.valuestack.push(w_nextitem)
        return next_instr

    def FOR_LOOP(f, oparg, *ignored):
        raise BytecodeCorruption, "old opcode, no longer in use"

    def SETUP_LOOP(f, offsettoend, next_instr, *ignored):
        block = LoopBlock(f, next_instr + offsettoend)
        f.blockstack.push(block)

    def SETUP_EXCEPT(f, offsettoend, next_instr, *ignored):
        block = ExceptBlock(f, next_instr + offsettoend)
        f.blockstack.push(block)

    def SETUP_FINALLY(f, offsettoend, next_instr, *ignored):
        block = FinallyBlock(f, next_instr + offsettoend)
        f.blockstack.push(block)

    def WITH_CLEANUP(f, *ignored):
        # see comment in END_FINALLY for stack state
        w_exitfunc = f.valuestack.pop()
        w_unroller = f.valuestack.top(2)
        unroller = f.space.interpclass_w(w_unroller)
        if isinstance(unroller, SApplicationException):
            operr = unroller.operr
            w_result = f.space.call_function(w_exitfunc,
                                             operr.w_type,
                                             operr.w_value,
                                             operr.application_traceback)
            if f.space.is_true(w_result):
                # __exit__() returned True -> Swallow the exception.
                f.valuestack.set_top(f.space.w_None, 2)
        else:
            f.space.call_function(w_exitfunc,
                                  f.space.w_None,
                                  f.space.w_None,
                                  f.space.w_None)
                      
    def call_function(f, oparg, w_star=None, w_starstar=None):
        n_arguments = oparg & 0xff
        n_keywords = (oparg>>8) & 0xff
        keywords = None
        if n_keywords:
            keywords = {}
            for i in range(n_keywords):
                w_value = f.valuestack.pop()
                w_key   = f.valuestack.pop()
                key = f.space.str_w(w_key)
                keywords[key] = w_value
        arguments = [None] * n_arguments
        for i in range(n_arguments - 1, -1, -1):
            arguments[i] = f.valuestack.pop()
        args = Arguments(f.space, arguments, keywords, w_star, w_starstar)
        w_function  = f.valuestack.pop()
        w_result = f.space.call_args(w_function, args)
        rstack.resume_point("call_function", f, returns=w_result)
        f.valuestack.push(w_result)
        
    def CALL_FUNCTION(f, oparg, *ignored):
        # XXX start of hack for performance
        if (oparg >> 8) & 0xff == 0:
            # Only positional arguments
            nargs = oparg & 0xff
            w_function = f.valuestack.top(nargs)
            try:
                w_result = f.space.call_valuestack(w_function, nargs, f.valuestack)
                rstack.resume_point("CALL_FUNCTION", f, nargs, returns=w_result)
            finally:
                f.valuestack.drop(nargs + 1)
            f.valuestack.push(w_result)
        # XXX end of hack for performance
        else:
            # general case
            f.call_function(oparg)

    def CALL_FUNCTION_VAR(f, oparg, *ignored):
        w_varargs = f.valuestack.pop()
        f.call_function(oparg, w_varargs)

    def CALL_FUNCTION_KW(f, oparg, *ignored):
        w_varkw = f.valuestack.pop()
        f.call_function(oparg, None, w_varkw)

    def CALL_FUNCTION_VAR_KW(f, oparg, *ignored):
        w_varkw = f.valuestack.pop()
        w_varargs = f.valuestack.pop()
        f.call_function(oparg, w_varargs, w_varkw)

    def MAKE_FUNCTION(f, numdefaults, *ignored):
        w_codeobj = f.valuestack.pop()
        codeobj = f.space.interp_w(PyCode, w_codeobj)
        defaultarguments = [f.valuestack.pop() for i in range(numdefaults)]
        defaultarguments.reverse()
        fn = function.Function(f.space, codeobj, f.w_globals, defaultarguments)
        f.valuestack.push(f.space.wrap(fn))

    def BUILD_SLICE(f, numargs, *ignored):
        if numargs == 3:
            w_step = f.valuestack.pop()
        elif numargs == 2:
            w_step = f.space.w_None
        else:
            raise BytecodeCorruption
        w_end   = f.valuestack.pop()
        w_start = f.valuestack.pop()
        w_slice = f.space.newslice(w_start, w_end, w_step)
        f.valuestack.push(w_slice)

    def LIST_APPEND(f, *ignored):
        w = f.valuestack.pop()
        v = f.valuestack.pop()
        f.space.call_method(v, 'append', w)

    def SET_LINENO(f, lineno, *ignored):
        pass

##     def EXTENDED_ARG(f, oparg, *ignored):
##         opcode = f.nextop()
##         oparg = oparg<<16 | f.nextarg()
##         fn = f.dispatch_table_w_arg[opcode]
##         if fn is None:
##             raise BytecodeCorruption
##         fn(f, oparg)

    def MISSING_OPCODE(f, oparg, next_instr, *ignored):
        ofs = next_instr - 1
        c = f.pycode.co_code[ofs]
        name = f.pycode.co_name
        raise BytecodeCorruption("unknown opcode, ofs=%d, code=%d, name=%s" %
                                 (ofs, ord(c), name) )

    STOP_CODE = MISSING_OPCODE


### ____________________________________________________________ ###


def cpython_tb():
   """NOT_RPYTHON"""
   import sys
   return sys.exc_info()[2]   
cpython_tb._annspecialcase_ = "override:ignore"

class Reraise(Exception):
    """Signal an application-level OperationError that should not grow
    a new traceback entry nor trigger the trace hook."""

class BytecodeCorruption(Exception):
    """Detected bytecode corruption.  Never caught; it's an error."""


### Frame Blocks ###

class SuspendedUnroller(Wrappable):
    """Abstract base class for interpreter-level objects that
    instruct the interpreter to change the control flow and the
    block stack.

    The concrete subclasses correspond to the various values WHY_XXX
    values of the why_code enumeration in ceval.c:

                WHY_NOT,        OK, not this one :-)
                WHY_EXCEPTION,  SApplicationException
                WHY_RERAISE,    implemented differently, see Reraise
                WHY_RETURN,     SReturnValue
                WHY_BREAK,      SBreakLoop
                WHY_CONTINUE,   SContinueLoop
                WHY_YIELD       not needed
    """
    def nomoreblocks(self):
        raise BytecodeCorruption("misplaced bytecode - should not return")
    # for the flow object space, a way to "pickle" and "unpickle" the
    # ControlFlowException by enumerating the Variables it contains.
    def state_unpack_variables(self, space):
        return []     # by default, overridden below
    def state_pack_variables(self, space, *values_w):
        assert len(values_w) == 0

class SReturnValue(SuspendedUnroller):
    """Signals a 'return' statement.
    Argument is the wrapped object to return."""
    kind = 0x01
    def __init__(self, w_returnvalue):
        self.w_returnvalue = w_returnvalue
    def nomoreblocks(self):
        return self.w_returnvalue
    def state_unpack_variables(self, space):
        return [self.w_returnvalue]
    def state_pack_variables(self, space, w_returnvalue):
        self.w_returnvalue = w_returnvalue

class SApplicationException(SuspendedUnroller):
    """Signals an application-level exception
    (i.e. an OperationException)."""
    kind = 0x02
    def __init__(self, operr):
        self.operr = operr
    def nomoreblocks(self):
        raise self.operr
    def state_unpack_variables(self, space):
        return [self.operr.w_type, self.operr.w_value]
    def state_pack_variables(self, space, w_type, w_value):
        self.operr = OperationError(w_type, w_value)

class SBreakLoop(SuspendedUnroller):
    """Signals a 'break' statement."""
    kind = 0x04
SBreakLoop.singleton = SBreakLoop()

class SContinueLoop(SuspendedUnroller):
    """Signals a 'continue' statement.
    Argument is the bytecode position of the beginning of the loop."""
    kind = 0x08
    def __init__(self, jump_to):
        self.jump_to = jump_to
    def state_unpack_variables(self, space):
        return [space.wrap(self.jump_to)]
    def state_pack_variables(self, space, w_jump_to):
        self.jump_to = space.int_w(w_jump_to)


class FrameBlock:

    """Abstract base class for frame blocks from the blockstack,
    used by the SETUP_XXX and POP_BLOCK opcodes."""

    def __init__(self, frame, handlerposition):
        self.handlerposition = handlerposition
        self.valuestackdepth = frame.valuestack.depth()

    def __eq__(self, other):
        return (self.__class__ is other.__class__ and
                self.handlerposition == other.handlerposition and
                self.valuestackdepth == other.valuestackdepth)

    def __ne__(self, other):
        return not (self == other)

    def __hash__(self):
        return hash((self.handlerposition, self.valuestackdepth))

    def cleanupstack(self, frame):
        for i in range(self.valuestackdepth, frame.valuestack.depth()):
            frame.valuestack.pop()

    def cleanup(self, frame):
        "Clean up a frame when we normally exit the block."
        self.cleanupstack(frame)

    # internal pickling interface, not using the standard protocol
    def _get_state_(self, space):
        w = space.wrap
        return space.newtuple([w(self._opname), w(self.handlerposition),
                               w(self.valuestackdepth)])

class LoopBlock(FrameBlock):
    """A loop block.  Stores the end-of-loop pointer in case of 'break'."""

    _opname = 'SETUP_LOOP'
    handling_mask = SBreakLoop.kind | SContinueLoop.kind

    def handle(self, frame, unroller):
        if isinstance(unroller, SContinueLoop):
            # re-push the loop block without cleaning up the value stack,
            # and jump to the beginning of the loop, stored in the
            # exception's argument
            frame.blockstack.push(self)
            return unroller.jump_to
        else:
            # jump to the end of the loop
            self.cleanupstack(frame)
            return self.handlerposition


class ExceptBlock(FrameBlock):
    """An try:except: block.  Stores the position of the exception handler."""

    _opname = 'SETUP_EXCEPT'
    handling_mask = SApplicationException.kind

    def handle(self, frame, unroller):
        # push the exception to the value stack for inspection by the
        # exception handler (the code after the except:)
        self.cleanupstack(frame)
        assert isinstance(unroller, SApplicationException)
        operationerr = unroller.operr
        if frame.space.full_exceptions:
            operationerr.normalize_exception(frame.space)
        # the stack setup is slightly different than in CPython:
        # instead of the traceback, we store the unroller object,
        # wrapped.
        frame.valuestack.push(frame.space.wrap(unroller))
        frame.valuestack.push(operationerr.w_value)
        frame.valuestack.push(operationerr.w_type)
        return self.handlerposition   # jump to the handler


class FinallyBlock(FrameBlock):
    """A try:finally: block.  Stores the position of the exception handler."""

    _opname = 'SETUP_FINALLY'
    handling_mask = -1     # handles every kind of SuspendedUnroller

    def cleanup(self, frame):
        # upon normal entry into the finally: part, the standard Python
        # bytecode pushes a single None for END_FINALLY.  In our case we
        # always push three values into the stack: the wrapped ctlflowexc,
        # the exception value and the exception type (which are all None
        # here).
        self.cleanupstack(frame)
        # one None already pushed by the bytecode
        frame.valuestack.push(frame.space.w_None)
        frame.valuestack.push(frame.space.w_None)

    def handle(self, frame, unroller):
        # any abnormal reason for unrolling a finally: triggers the end of
        # the block unrolling and the entering the finally: handler.
        # see comments in cleanup().
        self.cleanupstack(frame)
        frame.valuestack.push(frame.space.wrap(unroller))
        frame.valuestack.push(frame.space.w_None)
        frame.valuestack.push(frame.space.w_None)
        return self.handlerposition   # jump to the handler


block_classes = {'SETUP_LOOP': LoopBlock,
                 'SETUP_EXCEPT': ExceptBlock,
                 'SETUP_FINALLY': FinallyBlock}

### helpers written at the application-level ###
# Some of these functions are expected to be generally useful if other
# parts of the code need to do the same thing as a non-trivial opcode,
# like finding out which metaclass a new class should have.
# This is why they are not methods of PyFrame.
# There are also a couple of helpers that are methods, defined in the
# class above.

app = gateway.applevel(r'''
    """ applevel implementation of certain system properties, imports
    and other helpers"""
    import sys
    
    def sys_stdout():
        try: 
            return sys.stdout
        except AttributeError:
            raise RuntimeError("lost sys.stdout")

    def print_expr(obj):
        try:
            displayhook = sys.displayhook
        except AttributeError:
            raise RuntimeError("lost sys.displayhook")
        displayhook(obj)

    def print_item_to(x, stream):
        if file_softspace(stream, False):
           stream.write(" ")
        stream.write(str(x))

        # add a softspace unless we just printed a string which ends in a '\t'
        # or '\n' -- or more generally any whitespace character but ' '
        if isinstance(x, str) and x and x[-1].isspace() and x[-1]!=' ':
            return 
        # XXX add unicode handling
        file_softspace(stream, True)
    print_item_to._annspecialcase_ = "specialize:argtype(0)"

    def print_item(x):
        print_item_to(x, sys_stdout())
    print_item._annspecialcase_ = "flowspace:print_item"

    def print_newline_to(stream):
        stream.write("\n")
        file_softspace(stream, False)

    def print_newline():
        print_newline_to(sys_stdout())
    print_newline._annspecialcase_ = "flowspace:print_newline"

    def file_softspace(file, newflag):
        try:
            softspace = file.softspace
        except AttributeError:
            softspace = 0
        try:
            file.softspace = newflag
        except AttributeError:
            pass
        return softspace
''', filename=__file__)

sys_stdout      = app.interphook('sys_stdout')
print_expr      = app.interphook('print_expr')
print_item      = app.interphook('print_item')
print_item_to   = app.interphook('print_item_to')
print_newline   = app.interphook('print_newline')
print_newline_to= app.interphook('print_newline_to')
file_softspace  = app.interphook('file_softspace')

app = gateway.applevel(r'''
    def find_metaclass(bases, namespace, globals, builtin):
        if '__metaclass__' in namespace:
            return namespace['__metaclass__']
        elif len(bases) > 0:
            base = bases[0]
            if hasattr(base, '__class__'):
                return base.__class__
            else:
                return type(base)
        elif '__metaclass__' in globals:
            return globals['__metaclass__']
        else: 
            try: 
                return builtin.__metaclass__ 
            except AttributeError: 
                return type
''', filename=__file__)

find_metaclass  = app.interphook('find_metaclass')

app = gateway.applevel(r'''
    def import_all_from(module, into_locals):
        try:
            all = module.__all__
        except AttributeError:
            try:
                dict = module.__dict__
            except AttributeError:
                raise ImportError("from-import-* object has no __dict__ "
                                  "and no __all__")
            all = dict.keys()
            skip_leading_underscores = True
        else:
            skip_leading_underscores = False
        for name in all:
            if skip_leading_underscores and name[0]=='_':
                continue
            into_locals[name] = getattr(module, name)
''', filename=__file__)

import_all_from = app.interphook('import_all_from')

app = gateway.applevel(r'''
    def prepare_exec(f, prog, globals, locals, compile_flags, builtin, codetype):
        """Manipulate parameters to exec statement to (codeobject, dict, dict).
        """
        if (globals is None and locals is None and
            isinstance(prog, tuple) and
            (len(prog) == 2 or len(prog) == 3)):
            globals = prog[1]
            if len(prog) == 3:
                locals = prog[2]
            prog = prog[0]
        if globals is None:
            globals = f.f_globals
            if locals is None:
                locals = f.f_locals
        if locals is None:
            locals = globals

        if not isinstance(globals, dict):
            if not hasattr(globals, '__getitem__'):
                raise TypeError("exec: arg 2 must be a dictionary or None")
        try:
            globals['__builtins__']
        except KeyError:
            globals['__builtins__'] = builtin
        if not isinstance(locals, dict):
            if not hasattr(locals, '__getitem__'):
                raise TypeError("exec: arg 3 must be a dictionary or None")

        if not isinstance(prog, codetype):
            filename = '<string>'
            if not isinstance(prog, str):
                if isinstance(prog, basestring):
                    prog = str(prog)
                elif isinstance(prog, file):
                    filename = prog.name
                    prog = prog.read()
                else:
                    raise TypeError("exec: arg 1 must be a string, file, "
                                    "or code object")
            try:
                prog = compile(prog, filename, 'exec', compile_flags, 1)
            except SyntaxError, e: # exec SyntaxErrors have filename==None
               if len(e.args) == 2:
                   msg, loc = e.args
                   loc1 = (None,) + loc[1:]
                   e.args = msg, loc1
                   e.filename = None
               raise e
        return (prog, globals, locals)
''', filename=__file__)

prepare_exec    = app.interphook('prepare_exec')
