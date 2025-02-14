from pypy.rpython.memory.gctransform.transform import GCTransformer
from pypy.rpython.memory.gctransform.support import find_gc_ptrs_in_type, \
     get_rtti, ll_call_destructor, type_contains_pyobjs, var_ispyobj
from pypy.rpython.lltypesystem import lltype, llmemory, rffi, llgroup
from pypy.rpython import rmodel
from pypy.rpython.memory import gctypelayout
from pypy.rpython.memory.gc import marksweep
from pypy.rpython.memory.gcheader import GCHeaderBuilder
from pypy.rlib.rarithmetic import ovfcheck
from pypy.rlib import rgc
from pypy.rlib.debug import ll_assert
from pypy.rlib.objectmodel import we_are_translated
from pypy.translator.backendopt import graphanalyze
from pypy.translator.backendopt.support import var_needsgc
from pypy.annotation import model as annmodel
from pypy.rpython import annlowlevel
from pypy.rpython.rbuiltin import gen_cast
from pypy.rpython.memory.gctypelayout import ll_weakref_deref, WEAKREF
from pypy.rpython.memory.gctypelayout import convert_weakref_to, WEAKREFPTR
from pypy.rpython.memory.gctransform.log import log
from pypy.tool.sourcetools import func_with_new_name
from pypy.rpython.lltypesystem.lloperation import llop, LL_OPERATIONS
import sys, types


TYPE_ID = llgroup.HALFWORD

class CollectAnalyzer(graphanalyze.BoolGraphAnalyzer):

    def analyze_direct_call(self, graph, seen=None):
        try:
            func = graph.func
        except AttributeError:
            pass
        else:
            if getattr(func, '_gctransformer_hint_cannot_collect_', False):
                return False
            if getattr(func, '_gctransformer_hint_close_stack_', False):
                return True
        return graphanalyze.GraphAnalyzer.analyze_direct_call(self, graph,
                                                              seen)
    def analyze_external_call(self, op, seen=None):
        funcobj = op.args[0].value._obj
        if getattr(funcobj, 'random_effects_on_gcobjs', False):
            return True
        return graphanalyze.GraphAnalyzer.analyze_external_call(self, op,
                                                                seen)
    def analyze_simple_operation(self, op, graphinfo):
        if op.opname in ('malloc', 'malloc_varsize'):
            flags = op.args[1].value
            return flags['flavor'] == 'gc'
        else:
            return (op.opname in LL_OPERATIONS and
                    LL_OPERATIONS[op.opname].canmallocgc)

def find_initializing_stores(collect_analyzer, graph):
    from pypy.objspace.flow.model import mkentrymap
    entrymap = mkentrymap(graph)
    # a bit of a hackish analysis: if a block contains a malloc and check that
    # the result is not zero, then the block following the True link will
    # usually initialize the newly allocated object
    result = set()
    def find_in_block(block, mallocvars):
        for i, op in enumerate(block.operations):
            if op.opname in ("cast_pointer", "same_as"):
                if op.args[0] in mallocvars:
                    mallocvars[op.result] = True
            elif op.opname in ("setfield", "setarrayitem", "setinteriorfield"):
                TYPE = op.args[-1].concretetype
                if (op.args[0] in mallocvars and
                    isinstance(TYPE, lltype.Ptr) and
                    TYPE.TO._gckind == "gc"):
                    result.add(op)
            else:
                if collect_analyzer.analyze(op):
                    return
        for exit in block.exits:
            if len(entrymap[exit.target]) != 1:
                continue
            newmallocvars = {}
            for i, var in enumerate(exit.args):
                if var in mallocvars:
                    newmallocvars[exit.target.inputargs[i]] = True
            if newmallocvars:
                find_in_block(exit.target, newmallocvars)
    mallocnum = 0
    blockset = set(graph.iterblocks())
    while blockset:
        block = blockset.pop()
        if len(block.operations) < 2:
            continue
        mallocop = block.operations[-2]
        checkop = block.operations[-1]
        if not (mallocop.opname == "malloc" and
                checkop.opname == "ptr_nonzero" and
                mallocop.result is checkop.args[0] and
                block.exitswitch is checkop.result):
            continue
        rtti = get_rtti(mallocop.args[0].value)
        if rtti is not None and hasattr(rtti._obj, 'destructor_funcptr'):
            continue
        exits = [exit for exit in block.exits if exit.llexitcase]
        if len(exits) != 1:
            continue
        exit = exits[0]
        if len(entrymap[exit.target]) != 1:
            continue
        try:
            index = exit.args.index(mallocop.result)
        except ValueError:
            continue
        target = exit.target
        mallocvars = {target.inputargs[index]: True}
        mallocnum += 1
        find_in_block(target, mallocvars)
    #if result:
    #    print "found %s initializing stores in %s" % (len(result), graph.name)
    return result

def find_clean_setarrayitems(collect_analyzer, graph):
    result = set()
    for block in graph.iterblocks():
        cache = set()
        for op in block.operations:
            if op.opname == 'getarrayitem':
                cache.add((op.args[0], op.result))
            elif op.opname == 'setarrayitem':
                if (op.args[0], op.args[2]) in cache:
                    result.add(op)
            elif collect_analyzer.analyze(op):
                cache = set()
    return result

class FrameworkGCTransformer(GCTransformer):
    root_stack_depth = None    # for tests to override

    def __init__(self, translator):
        from pypy.rpython.memory.gc.base import choose_gc_from_config
        from pypy.rpython.memory.gc.base import ARRAY_TYPEID_MAP
        from pypy.rpython.memory.gc import inspector

        super(FrameworkGCTransformer, self).__init__(translator, inline=True)
        if hasattr(self, 'GC_PARAMS'):
            # for tests: the GC choice can be specified as class attributes
            from pypy.rpython.memory.gc.marksweep import MarkSweepGC
            GCClass = getattr(self, 'GCClass', MarkSweepGC)
            GC_PARAMS = self.GC_PARAMS
        else:
            # for regular translation: pick the GC from the config
            GCClass, GC_PARAMS = choose_gc_from_config(translator.config)

        if hasattr(translator, '_jit2gc'):
            self.layoutbuilder = translator._jit2gc['layoutbuilder']
        else:
            self.layoutbuilder = TransformerLayoutBuilder(translator, GCClass)
        self.layoutbuilder.transformer = self
        self.get_type_id = self.layoutbuilder.get_type_id

        # set up GCData with the llgroup from the layoutbuilder, which
        # will grow as more TYPE_INFO members are added to it
        gcdata = gctypelayout.GCData(self.layoutbuilder.type_info_group)

        # initialize the following two fields with a random non-NULL address,
        # to make the annotator happy.  The fields are patched in finish()
        # to point to a real array.
        foo = lltype.malloc(lltype.FixedSizeArray(llmemory.Address, 1),
                            immortal=True, zero=True)
        a_random_address = llmemory.cast_ptr_to_adr(foo)
        gcdata.static_root_start = a_random_address      # patched in finish()
        gcdata.static_root_nongcend = a_random_address   # patched in finish()
        gcdata.static_root_end = a_random_address        # patched in finish()
        gcdata.max_type_id = 13                          # patched in finish()
        gcdata.typeids_z = a_random_address              # patched in finish()
        self.gcdata = gcdata
        self.malloc_fnptr_cache = {}

        gcdata.gc = GCClass(translator.config.translation, **GC_PARAMS)
        root_walker = self.build_root_walker()
        self.root_walker = root_walker
        gcdata.set_query_functions(gcdata.gc)
        gcdata.gc.set_root_walker(root_walker)
        self.num_pushs = 0
        self.write_barrier_calls = 0
        self.write_barrier_from_array_calls = 0

        def frameworkgc_setup():
            # run-time initialization code
            root_walker.setup_root_walker()
            gcdata.gc.setup()
            gcdata.gc.post_setup()

        def frameworkgc__teardown():
            # run-time teardown code for tests!
            gcdata.gc._teardown()

        bk = self.translator.annotator.bookkeeper
        r_typeid16 = rffi.platform.numbertype_to_rclass[TYPE_ID]
        s_typeid16 = annmodel.SomeInteger(knowntype=r_typeid16)

        # the point of this little dance is to not annotate
        # self.gcdata.static_root_xyz as constants. XXX is it still needed??
        data_classdef = bk.getuniqueclassdef(gctypelayout.GCData)
        data_classdef.generalize_attr(
            'static_root_start',
            annmodel.SomeAddress())
        data_classdef.generalize_attr(
            'static_root_nongcend',
            annmodel.SomeAddress())
        data_classdef.generalize_attr(
            'static_root_end',
            annmodel.SomeAddress())
        data_classdef.generalize_attr(
            'max_type_id',
            annmodel.SomeInteger())
        data_classdef.generalize_attr(
            'typeids_z',
            annmodel.SomeAddress())

        annhelper = annlowlevel.MixLevelHelperAnnotator(self.translator.rtyper)

        def getfn(ll_function, args_s, s_result, inline=False,
                  minimal_transform=True):
            graph = annhelper.getgraph(ll_function, args_s, s_result)
            if minimal_transform:
                self.need_minimal_transform(graph)
            if inline:
                self.graphs_to_inline[graph] = True
            return annhelper.graph2const(graph)

        self.frameworkgc_setup_ptr = getfn(frameworkgc_setup, [],
                                           annmodel.s_None)
        # for tests
        self.frameworkgc__teardown_ptr = getfn(frameworkgc__teardown, [],
                                               annmodel.s_None)
        
        if root_walker.need_root_stack:
            self.incr_stack_ptr = getfn(root_walker.incr_stack,
                                       [annmodel.SomeInteger()],
                                       annmodel.SomeAddress(),
                                       inline = True)
            self.decr_stack_ptr = getfn(root_walker.decr_stack,
                                       [annmodel.SomeInteger()],
                                       annmodel.SomeAddress(),
                                       inline = True)
        else:
            self.incr_stack_ptr = None
            self.decr_stack_ptr = None
        self.weakref_deref_ptr = self.inittime_helper(
            ll_weakref_deref, [llmemory.WeakRefPtr], llmemory.Address)
        
        classdef = bk.getuniqueclassdef(GCClass)
        s_gc = annmodel.SomeInstance(classdef)
        s_gcref = annmodel.SomePtr(llmemory.GCREF)

        malloc_fixedsize_clear_meth = GCClass.malloc_fixedsize_clear.im_func
        self.malloc_fixedsize_clear_ptr = getfn(
            malloc_fixedsize_clear_meth,
            [s_gc, s_typeid16,
             annmodel.SomeInteger(nonneg=True),
             annmodel.SomeBool(),
             annmodel.SomeBool()], s_gcref,
            inline = False)
        if hasattr(GCClass, 'malloc_fixedsize'):
            malloc_fixedsize_meth = GCClass.malloc_fixedsize.im_func
            self.malloc_fixedsize_ptr = getfn(
                malloc_fixedsize_meth,
                [s_gc, s_typeid16,
                 annmodel.SomeInteger(nonneg=True),
                 annmodel.SomeBool(),
                 annmodel.SomeBool()], s_gcref,
                inline = False)
        else:
            malloc_fixedsize_meth = None
            self.malloc_fixedsize_ptr = self.malloc_fixedsize_clear_ptr
##         self.malloc_varsize_ptr = getfn(
##             GCClass.malloc_varsize.im_func,
##             [s_gc] + [annmodel.SomeInteger(nonneg=True) for i in range(5)]
##             + [annmodel.SomeBool()], s_gcref)
        self.malloc_varsize_clear_ptr = getfn(
            GCClass.malloc_varsize_clear.im_func,
            [s_gc, s_typeid16]
            + [annmodel.SomeInteger(nonneg=True) for i in range(4)], s_gcref)
        self.collect_ptr = getfn(GCClass.collect.im_func,
            [s_gc, annmodel.SomeInteger()], annmodel.s_None)
        self.can_move_ptr = getfn(GCClass.can_move.im_func,
                                  [s_gc, annmodel.SomeAddress()],
                                  annmodel.SomeBool())

        if hasattr(GCClass, 'shrink_array'):
            self.shrink_array_ptr = getfn(
                GCClass.shrink_array.im_func,
                [s_gc, annmodel.SomeAddress(),
                 annmodel.SomeInteger(nonneg=True)], annmodel.s_Bool)
        else:
            self.shrink_array_ptr = None

        if hasattr(GCClass, 'assume_young_pointers'):
            # xxx should really be a noop for gcs without generations
            self.assume_young_pointers_ptr = getfn(
                GCClass.assume_young_pointers.im_func,
                [s_gc, annmodel.SomeAddress()],
                annmodel.s_None)

        if hasattr(GCClass, 'heap_stats'):
            self.heap_stats_ptr = getfn(GCClass.heap_stats.im_func,
                    [s_gc], annmodel.SomePtr(lltype.Ptr(ARRAY_TYPEID_MAP)),
                    minimal_transform=False)
            self.get_member_index_ptr = getfn(
                GCClass.get_member_index.im_func,
                [s_gc, annmodel.SomeInteger(knowntype=llgroup.r_halfword)],
                annmodel.SomeInteger())

        if hasattr(GCClass, 'writebarrier_before_copy'):
            self.wb_before_copy_ptr = \
                    getfn(GCClass.writebarrier_before_copy.im_func,
                    [s_gc] + [annmodel.SomeAddress()] * 2 +
                    [annmodel.SomeInteger()] * 3, annmodel.SomeBool())
        elif GCClass.needs_write_barrier:
            raise NotImplementedError("GC needs write barrier, but does not provide writebarrier_before_copy functionality")

        # in some GCs we can inline the common case of
        # malloc_fixedsize(typeid, size, True, False, False)
        if getattr(GCClass, 'inline_simple_malloc', False):
            # make a copy of this function so that it gets annotated
            # independently and the constants are folded inside
            if malloc_fixedsize_meth is None:
                malloc_fast_meth = malloc_fixedsize_clear_meth
                self.malloc_fast_is_clearing = True
            else:
                malloc_fast_meth = malloc_fixedsize_meth
                self.malloc_fast_is_clearing = False
            malloc_fast = func_with_new_name(
                malloc_fast_meth,
                "malloc_fast")
            s_False = annmodel.SomeBool(); s_False.const = False
            self.malloc_fast_ptr = getfn(
                malloc_fast,
                [s_gc, s_typeid16,
                 annmodel.SomeInteger(nonneg=True),
                 s_False, s_False], s_gcref,
                inline = True)
        else:
            self.malloc_fast_ptr = None

        # in some GCs we can also inline the common case of
        # malloc_varsize(typeid, length, (3 constant sizes), True, False)
        if getattr(GCClass, 'inline_simple_malloc_varsize', False):
            # make a copy of this function so that it gets annotated
            # independently and the constants are folded inside
            malloc_varsize_clear_fast = func_with_new_name(
                GCClass.malloc_varsize_clear.im_func,
                "malloc_varsize_clear_fast")
            s_False = annmodel.SomeBool(); s_False.const = False
            self.malloc_varsize_clear_fast_ptr = getfn(
                malloc_varsize_clear_fast,
                [s_gc, s_typeid16,
                 annmodel.SomeInteger(nonneg=True),
                 annmodel.SomeInteger(nonneg=True),
                 annmodel.SomeInteger(nonneg=True),
                 annmodel.SomeInteger(nonneg=True)], s_gcref,
                inline = True)
        else:
            self.malloc_varsize_clear_fast_ptr = None

        if getattr(GCClass, 'malloc_varsize_nonmovable', False):
            malloc_nonmovable = func_with_new_name(
                GCClass.malloc_varsize_nonmovable.im_func,
                "malloc_varsize_nonmovable")
            self.malloc_varsize_nonmovable_ptr = getfn(
                malloc_nonmovable,
                [s_gc, s_typeid16,
                 annmodel.SomeInteger(nonneg=True)], s_gcref)
        else:
            self.malloc_varsize_nonmovable_ptr = None

        if getattr(GCClass, 'raw_malloc_memory_pressure', False):
            def raw_malloc_memory_pressure(length, itemsize):
                totalmem = length * itemsize
                if totalmem > 0:
                    gcdata.gc.raw_malloc_memory_pressure(totalmem)
                #else: probably an overflow -- the following rawmalloc
                #      will fail then
            self.raw_malloc_memory_pressure_ptr = getfn(
                raw_malloc_memory_pressure,
                [annmodel.SomeInteger(), annmodel.SomeInteger()],
                annmodel.s_None, minimal_transform = False)

        self.identityhash_ptr = getfn(GCClass.identityhash.im_func,
                                      [s_gc, s_gcref],
                                      annmodel.SomeInteger(),
                                      minimal_transform=False)
        if getattr(GCClass, 'obtain_free_space', False):
            self.obtainfreespace_ptr = getfn(GCClass.obtain_free_space.im_func,
                                             [s_gc, annmodel.SomeInteger()],
                                             annmodel.SomeAddress())

        if GCClass.moving_gc:
            self.id_ptr = getfn(GCClass.id.im_func,
                                [s_gc, s_gcref], annmodel.SomeInteger(),
                                inline = False,
                                minimal_transform = False)
        else:
            self.id_ptr = None

        self.get_rpy_roots_ptr = getfn(inspector.get_rpy_roots,
                                       [s_gc],
                                       rgc.s_list_of_gcrefs(),
                                       minimal_transform=False)
        self.get_rpy_referents_ptr = getfn(inspector.get_rpy_referents,
                                           [s_gc, s_gcref],
                                           rgc.s_list_of_gcrefs(),
                                           minimal_transform=False)
        self.get_rpy_memory_usage_ptr = getfn(inspector.get_rpy_memory_usage,
                                              [s_gc, s_gcref],
                                              annmodel.SomeInteger(),
                                              minimal_transform=False)
        self.get_rpy_type_index_ptr = getfn(inspector.get_rpy_type_index,
                                            [s_gc, s_gcref],
                                            annmodel.SomeInteger(),
                                            minimal_transform=False)
        self.is_rpy_instance_ptr = getfn(inspector.is_rpy_instance,
                                         [s_gc, s_gcref],
                                         annmodel.SomeBool(),
                                         minimal_transform=False)
        self.dump_rpy_heap_ptr = getfn(inspector.dump_rpy_heap,
                                       [s_gc, annmodel.SomeInteger()],
                                       annmodel.s_Bool,
                                       minimal_transform=False)
        self.get_typeids_z_ptr = getfn(inspector.get_typeids_z,
                                       [s_gc],
                                       annmodel.SomePtr(
                                           lltype.Ptr(rgc.ARRAY_OF_CHAR)),
                                       minimal_transform=False)

        self.set_max_heap_size_ptr = getfn(GCClass.set_max_heap_size.im_func,
                                           [s_gc,
                                            annmodel.SomeInteger(nonneg=True)],
                                           annmodel.s_None)

        self.write_barrier_ptr = None
        self.write_barrier_from_array_ptr = None
        if GCClass.needs_write_barrier:
            self.write_barrier_ptr = getfn(GCClass.write_barrier.im_func,
                                           [s_gc,
                                            annmodel.SomeAddress(),
                                            annmodel.SomeAddress()],
                                           annmodel.s_None,
                                           inline=True)
            func = getattr(gcdata.gc, 'remember_young_pointer', None)
            if func is not None:
                # func should not be a bound method, but a real function
                assert isinstance(func, types.FunctionType)
                self.write_barrier_failing_case_ptr = getfn(func,
                                               [annmodel.SomeAddress(),
                                                annmodel.SomeAddress()],
                                               annmodel.s_None)
            func = getattr(GCClass, 'write_barrier_from_array', None)
            if func is not None:
                self.write_barrier_from_array_ptr = getfn(func.im_func,
                                           [s_gc,
                                            annmodel.SomeAddress(),
                                            annmodel.SomeAddress(),
                                            annmodel.SomeInteger()],
                                           annmodel.s_None,
                                           inline=True)
                func = getattr(gcdata.gc, 'remember_young_pointer_from_array3',
                               None)
                if func is not None:
                    # func should not be a bound method, but a real function
                    assert isinstance(func, types.FunctionType)
                    self.write_barrier_from_array_failing_case_ptr = \
                                             getfn(func,
                                                   [annmodel.SomeAddress(),
                                                    annmodel.SomeInteger(),
                                                    annmodel.SomeAddress()],
                                                   annmodel.s_None)
        self.statistics_ptr = getfn(GCClass.statistics.im_func,
                                    [s_gc, annmodel.SomeInteger()],
                                    annmodel.SomeInteger())

        # thread support
        if translator.config.translation.continuation:
            root_walker.need_stacklet_support(self, getfn)
        if translator.config.translation.thread:
            root_walker.need_thread_support(self, getfn)

        self.layoutbuilder.encode_type_shapes_now()

        annhelper.finish()   # at this point, annotate all mix-level helpers
        annhelper.backend_optimize()

        self.collect_analyzer = CollectAnalyzer(self.translator)
        self.collect_analyzer.analyze_all()

        s_gc = self.translator.annotator.bookkeeper.valueoftype(GCClass)
        r_gc = self.translator.rtyper.getrepr(s_gc)
        self.c_const_gc = rmodel.inputconst(r_gc, self.gcdata.gc)
        s_gc_data = self.translator.annotator.bookkeeper.valueoftype(
            gctypelayout.GCData)
        r_gc_data = self.translator.rtyper.getrepr(s_gc_data)
        self.c_const_gcdata = rmodel.inputconst(r_gc_data, self.gcdata)
        self.malloc_zero_filled = GCClass.malloc_zero_filled

        HDR = self.HDR = self.gcdata.gc.gcheaderbuilder.HDR

        size_gc_header = self.gcdata.gc.gcheaderbuilder.size_gc_header
        vtableinfo = (HDR, size_gc_header, self.gcdata.gc.typeid_is_in_field)
        self.c_vtableinfo = rmodel.inputconst(lltype.Void, vtableinfo)
        tig = self.layoutbuilder.type_info_group._as_ptr()
        self.c_type_info_group = rmodel.inputconst(lltype.typeOf(tig), tig)
        sko = llmemory.sizeof(gcdata.TYPE_INFO)
        self.c_vtinfo_skip_offset = rmodel.inputconst(lltype.typeOf(sko), sko)

    def build_root_walker(self):
        from pypy.rpython.memory.gctransform import shadowstack
        return shadowstack.ShadowStackRootWalker(self)

    def consider_constant(self, TYPE, value):
        self.layoutbuilder.consider_constant(TYPE, value, self.gcdata.gc)

    #def get_type_id(self, TYPE):
    #    this method is attached to the instance and redirects to
    #    layoutbuilder.get_type_id().

    def special_funcptr_for_type(self, TYPE):
        return self.layoutbuilder.special_funcptr_for_type(TYPE)

    def gc_header_for(self, obj, needs_hash=False):
        hdr = self.gcdata.gc.gcheaderbuilder.header_of_object(obj)
        HDR = self.HDR
        withhash, flag = self.gcdata.gc.withhash_flag_is_in_field
        x = getattr(hdr, withhash)
        TYPE = lltype.typeOf(x)
        x = lltype.cast_primitive(lltype.Signed, x)
        if needs_hash:
            x |= flag       # set the flag in the header
        else:
            x &= ~flag      # clear the flag in the header
        x = lltype.cast_primitive(TYPE, x)
        setattr(hdr, withhash, x)
        return hdr

    def get_hash_offset(self, T):
        type_id = self.get_type_id(T)
        assert not self.gcdata.q_is_varsize(type_id)
        return self.gcdata.q_fixed_size(type_id)

    def finish_tables(self):
        group = self.layoutbuilder.close_table()
        log.info("assigned %s typeids" % (len(group.members), ))
        log.info("added %s push/pop stack root instructions" % (
                     self.num_pushs, ))
        if self.write_barrier_ptr:
            log.info("inserted %s write barrier calls" % (
                         self.write_barrier_calls, ))
        if self.write_barrier_from_array_ptr:
            log.info("inserted %s write_barrier_from_array calls" % (
                         self.write_barrier_from_array_calls, ))

        # XXX because we call inputconst already in replace_malloc, we can't
        # modify the instance, we have to modify the 'rtyped instance'
        # instead.  horrors.  is there a better way?

        s_gcdata = self.translator.annotator.bookkeeper.immutablevalue(
            self.gcdata)
        r_gcdata = self.translator.rtyper.getrepr(s_gcdata)
        ll_instance = rmodel.inputconst(r_gcdata, self.gcdata).value

        addresses_of_static_ptrs = (
            self.layoutbuilder.addresses_of_static_ptrs_in_nongc +
            self.layoutbuilder.addresses_of_static_ptrs)
        log.info("found %s static roots" % (len(addresses_of_static_ptrs), ))
        ll_static_roots_inside = lltype.malloc(lltype.Array(llmemory.Address),
                                               len(addresses_of_static_ptrs),
                                               immortal=True)

        for i in range(len(addresses_of_static_ptrs)):
            ll_static_roots_inside[i] = addresses_of_static_ptrs[i]
        ll_instance.inst_static_root_start = llmemory.cast_ptr_to_adr(ll_static_roots_inside) + llmemory.ArrayItemsOffset(lltype.Array(llmemory.Address))
        ll_instance.inst_static_root_nongcend = ll_instance.inst_static_root_start + llmemory.sizeof(llmemory.Address) * len(self.layoutbuilder.addresses_of_static_ptrs_in_nongc)
        ll_instance.inst_static_root_end = ll_instance.inst_static_root_start + llmemory.sizeof(llmemory.Address) * len(addresses_of_static_ptrs)
        newgcdependencies = []
        newgcdependencies.append(ll_static_roots_inside)
        ll_instance.inst_max_type_id = len(group.members)
        typeids_z = self.write_typeid_list()
        ll_typeids_z = lltype.malloc(rgc.ARRAY_OF_CHAR,
                                     len(typeids_z),
                                     immortal=True)
        for i in range(len(typeids_z)):
            ll_typeids_z[i] = typeids_z[i]
        ll_instance.inst_typeids_z = llmemory.cast_ptr_to_adr(ll_typeids_z)
        newgcdependencies.append(ll_typeids_z)
        return newgcdependencies

    def get_finish_tables(self):
        # We must first make sure that the type_info_group's members
        # are all followed.  Do it repeatedly while new members show up.
        # Once it is really done, do finish_tables().
        seen = 0
        while seen < len(self.layoutbuilder.type_info_group.members):
            curtotal = len(self.layoutbuilder.type_info_group.members)
            yield self.layoutbuilder.type_info_group.members[seen:curtotal]
            seen = curtotal
        yield self.finish_tables()

    def write_typeid_list(self):
        """write out the list of type ids together with some info"""
        from pypy.tool.udir import udir
        # XXX not ideal since it is not per compilation, but per run
        # XXX argh argh, this only gives the member index but not the
        #     real typeid, which is a complete mess to obtain now...
        all_ids = self.layoutbuilder.id_of_type.items()
        all_ids = [(typeinfo.index, TYPE) for (TYPE, typeinfo) in all_ids]
        all_ids = dict(all_ids)
        f = udir.join("typeids.txt").open("w")
        for index in range(len(self.layoutbuilder.type_info_group.members)):
            f.write("member%-4d %s\n" % (index, all_ids.get(index, '?')))
        f.close()
        try:
            import zlib
            return zlib.compress(udir.join("typeids.txt").read(), 9)
        except ImportError:
            return ''

    def transform_graph(self, graph):
        func = getattr(graph, 'func', None)
        if func and getattr(func, '_gc_no_collect_', False):
            if self.collect_analyzer.analyze_direct_call(graph):
                raise Exception("'no_collect' function can trigger collection:"
                                " %s" % func)
            
        if self.write_barrier_ptr:
            self.clean_sets = (
                find_initializing_stores(self.collect_analyzer, graph))
            if self.gcdata.gc.can_optimize_clean_setarrayitems():
                self.clean_sets = self.clean_sets.union(
                    find_clean_setarrayitems(self.collect_analyzer, graph))
        super(FrameworkGCTransformer, self).transform_graph(graph)
        if self.write_barrier_ptr:
            self.clean_sets = None

    def gct_direct_call(self, hop):
        if self.collect_analyzer.analyze(hop.spaceop):
            livevars = self.push_roots(hop)
            self.default(hop)
            self.pop_roots(hop, livevars)
        else:
            self.default(hop)
            if hop.spaceop.opname == "direct_call":
                self.mark_call_cannotcollect(hop, hop.spaceop.args[0])

    def mark_call_cannotcollect(self, hop, name):
        pass

    gct_indirect_call = gct_direct_call

    def gct_fv_gc_malloc(self, hop, flags, TYPE, *args):
        op = hop.spaceop
        flavor = flags['flavor']

        PTRTYPE = op.result.concretetype
        assert PTRTYPE.TO == TYPE
        type_id = self.get_type_id(TYPE)

        c_type_id = rmodel.inputconst(TYPE_ID, type_id)
        info = self.layoutbuilder.get_info(type_id)
        c_size = rmodel.inputconst(lltype.Signed, info.fixedsize)
        kind_and_fptr = self.special_funcptr_for_type(TYPE)
        has_finalizer = (kind_and_fptr is not None and
                         kind_and_fptr[0] == "finalizer")
        c_has_finalizer = rmodel.inputconst(lltype.Bool, has_finalizer)

        if not op.opname.endswith('_varsize') and not flags.get('varsize'):
            #malloc_ptr = self.malloc_fixedsize_ptr
            zero = flags.get('zero', False)
            if (self.malloc_fast_ptr is not None and
                not c_has_finalizer.value and
                (self.malloc_fast_is_clearing or not zero)):
                malloc_ptr = self.malloc_fast_ptr
            elif zero:
                malloc_ptr = self.malloc_fixedsize_clear_ptr
            else:
                malloc_ptr = self.malloc_fixedsize_ptr
            args = [self.c_const_gc, c_type_id, c_size,
                    c_has_finalizer, rmodel.inputconst(lltype.Bool, False)]
        else:
            assert not c_has_finalizer.value
            info_varsize = self.layoutbuilder.get_info_varsize(type_id)
            v_length = op.args[-1]
            c_ofstolength = rmodel.inputconst(lltype.Signed,
                                              info_varsize.ofstolength)
            c_varitemsize = rmodel.inputconst(lltype.Signed,
                                              info_varsize.varitemsize)
            if flags.get('nonmovable') and self.malloc_varsize_nonmovable_ptr:
                # we don't have tests for such cases, let's fail
                # explicitely
                malloc_ptr = self.malloc_varsize_nonmovable_ptr
                args = [self.c_const_gc, c_type_id, v_length]
            else:
                if self.malloc_varsize_clear_fast_ptr is not None:
                    malloc_ptr = self.malloc_varsize_clear_fast_ptr
                else:
                    malloc_ptr = self.malloc_varsize_clear_ptr
                args = [self.c_const_gc, c_type_id, v_length, c_size,
                        c_varitemsize, c_ofstolength]
        livevars = self.push_roots(hop)
        v_result = hop.genop("direct_call", [malloc_ptr] + args,
                             resulttype=llmemory.GCREF)
        self.pop_roots(hop, livevars)
        return v_result

    gct_fv_gc_malloc_varsize = gct_fv_gc_malloc

    def gct_gc__collect(self, hop):
        op = hop.spaceop
        if len(op.args) == 1:
            v_gen = op.args[0]
        else:
            # pick a number larger than expected different gc gens :-)
            v_gen = rmodel.inputconst(lltype.Signed, 9)
        livevars = self.push_roots(hop)
        hop.genop("direct_call", [self.collect_ptr, self.c_const_gc, v_gen],
                  resultvar=op.result)
        self.pop_roots(hop, livevars)

    def gct_gc_can_move(self, hop):
        op = hop.spaceop
        v_addr = hop.genop('cast_ptr_to_adr',
                           [op.args[0]], resulttype=llmemory.Address)
        hop.genop("direct_call", [self.can_move_ptr, self.c_const_gc, v_addr],
                  resultvar=op.result)

    def gct_shrink_array(self, hop):
        if self.shrink_array_ptr is None:
            return GCTransformer.gct_shrink_array(self, hop)
        op = hop.spaceop
        v_addr = hop.genop('cast_ptr_to_adr',
                           [op.args[0]], resulttype=llmemory.Address)
        v_length = op.args[1]
        hop.genop("direct_call", [self.shrink_array_ptr, self.c_const_gc,
                                  v_addr, v_length],
                  resultvar=op.result)

    def gct_gc_assume_young_pointers(self, hop):
        if not hasattr(self, 'assume_young_pointers_ptr'):
            return
        op = hop.spaceop
        v_addr = op.args[0]
        if v_addr.concretetype != llmemory.Address:
            v_addr = hop.genop('cast_ptr_to_adr',
                               [v_addr], resulttype=llmemory.Address)
        hop.genop("direct_call", [self.assume_young_pointers_ptr,
                                  self.c_const_gc, v_addr])

    def gct_gc_heap_stats(self, hop):
        if not hasattr(self, 'heap_stats_ptr'):
            return GCTransformer.gct_gc_heap_stats(self, hop)
        op = hop.spaceop
        livevars = self.push_roots(hop)
        hop.genop("direct_call", [self.heap_stats_ptr, self.c_const_gc],
                  resultvar=op.result)
        self.pop_roots(hop, livevars)

    def gct_get_member_index(self, hop):
        op = hop.spaceop
        v_typeid = op.args[0]
        hop.genop("direct_call", [self.get_member_index_ptr, self.c_const_gc,
                                  v_typeid], resultvar=op.result)

    def _gc_adr_of_gc_attr(self, hop, attrname):
        if getattr(self.gcdata.gc, attrname, None) is None:
            raise NotImplementedError("gc_adr_of_%s only for generational gcs"
                                      % (attrname,))
        op = hop.spaceop
        ofs = llmemory.offsetof(self.c_const_gc.concretetype.TO,
                                'inst_' + attrname)
        c_ofs = rmodel.inputconst(lltype.Signed, ofs)
        v_gc_adr = hop.genop('cast_ptr_to_adr', [self.c_const_gc],
                             resulttype=llmemory.Address)
        hop.genop('adr_add', [v_gc_adr, c_ofs], resultvar=op.result)

    def gct_gc_adr_of_nursery_free(self, hop):
        self._gc_adr_of_gc_attr(hop, 'nursery_free')
    def gct_gc_adr_of_nursery_top(self, hop):
        self._gc_adr_of_gc_attr(hop, 'nursery_top')

    def _gc_adr_of_gcdata_attr(self, hop, attrname):
        op = hop.spaceop
        ofs = llmemory.offsetof(self.c_const_gcdata.concretetype.TO,
                                'inst_' + attrname)
        c_ofs = rmodel.inputconst(lltype.Signed, ofs)
        v_gcdata_adr = hop.genop('cast_ptr_to_adr', [self.c_const_gcdata],
                                 resulttype=llmemory.Address)
        hop.genop('adr_add', [v_gcdata_adr, c_ofs], resultvar=op.result)

    def gct_gc_adr_of_root_stack_base(self, hop):
        self._gc_adr_of_gcdata_attr(hop, 'root_stack_base')
    def gct_gc_adr_of_root_stack_top(self, hop):
        self._gc_adr_of_gcdata_attr(hop, 'root_stack_top')

    def gct_gc_shadowstackref_new(self, hop):
        op = hop.spaceop
        livevars = self.push_roots(hop)
        hop.genop("direct_call", [self.root_walker.gc_shadowstackref_new_ptr],
                  resultvar=op.result)
        self.pop_roots(hop, livevars)

    def gct_gc_shadowstackref_context(self, hop):
        op = hop.spaceop
        hop.genop("direct_call",
                  [self.root_walker.gc_shadowstackref_context_ptr, op.args[0]],
                  resultvar=op.result)

    def gct_gc_shadowstackref_destroy(self, hop):
        op = hop.spaceop
        hop.genop("direct_call",
                  [self.root_walker.gc_shadowstackref_destroy_ptr, op.args[0]])

    def gct_gc_save_current_state_away(self, hop):
        op = hop.spaceop
        hop.genop("direct_call",
                  [self.root_walker.gc_save_current_state_away_ptr,
                   op.args[0], op.args[1]])

    def gct_gc_forget_current_state(self, hop):
        hop.genop("direct_call",
                  [self.root_walker.gc_forget_current_state_ptr])

    def gct_gc_restore_state_from(self, hop):
        op = hop.spaceop
        hop.genop("direct_call",
                  [self.root_walker.gc_restore_state_from_ptr,
                   op.args[0]])

    def gct_gc_start_fresh_new_state(self, hop):
        hop.genop("direct_call",
                  [self.root_walker.gc_start_fresh_new_state_ptr])

    def gct_gc_x_swap_pool(self, hop):
        raise NotImplementedError("old operation deprecated")
    def gct_gc_x_clone(self, hop):
        raise NotImplementedError("old operation deprecated")
    def gct_gc_x_size_header(self, hop):
        raise NotImplementedError("old operation deprecated")

    def gct_do_malloc_fixedsize_clear(self, hop):
        # used by the JIT (see pypy.jit.backend.llsupport.gc)
        op = hop.spaceop
        [v_typeid, v_size,
         v_has_finalizer, v_contains_weakptr] = op.args
        livevars = self.push_roots(hop)
        hop.genop("direct_call",
                  [self.malloc_fixedsize_clear_ptr, self.c_const_gc,
                   v_typeid, v_size,
                   v_has_finalizer, v_contains_weakptr],
                  resultvar=op.result)
        self.pop_roots(hop, livevars)

    def gct_do_malloc_varsize_clear(self, hop):
        # used by the JIT (see pypy.jit.backend.llsupport.gc)
        op = hop.spaceop
        [v_typeid, v_length, v_size, v_itemsize,
         v_offset_to_length] = op.args
        livevars = self.push_roots(hop)
        hop.genop("direct_call",
                  [self.malloc_varsize_clear_ptr, self.c_const_gc,
                   v_typeid, v_length, v_size, v_itemsize,
                   v_offset_to_length],
                  resultvar=op.result)
        self.pop_roots(hop, livevars)

    def gct_get_write_barrier_failing_case(self, hop):
        op = hop.spaceop
        hop.genop("same_as",
                  [self.write_barrier_failing_case_ptr],
                  resultvar=op.result)

    def gct_get_write_barrier_from_array_failing_case(self, hop):
        op = hop.spaceop
        v = getattr(self, 'write_barrier_from_array_failing_case_ptr',
                    lltype.nullptr(op.result.concretetype.TO))
        hop.genop("same_as", [v], resultvar=op.result)

    def gct_zero_gc_pointers_inside(self, hop):
        if not self.malloc_zero_filled:
            v_ob = hop.spaceop.args[0]
            TYPE = v_ob.concretetype.TO
            gen_zero_gc_pointers(TYPE, v_ob, hop.llops)

    def gct_gc_writebarrier_before_copy(self, hop):
        op = hop.spaceop
        if not hasattr(self, 'wb_before_copy_ptr'):
            # no write barrier needed in that case
            hop.genop("same_as",
                      [rmodel.inputconst(lltype.Bool, True)],
                      resultvar=op.result)
            return
        source_addr = hop.genop('cast_ptr_to_adr', [op.args[0]],
                                resulttype=llmemory.Address)
        dest_addr = hop.genop('cast_ptr_to_adr', [op.args[1]],
                                resulttype=llmemory.Address)
        hop.genop('direct_call', [self.wb_before_copy_ptr, self.c_const_gc,
                                  source_addr, dest_addr] + op.args[2:],
                  resultvar=op.result)

    def gct_weakref_create(self, hop):
        op = hop.spaceop

        type_id = self.get_type_id(WEAKREF)

        c_type_id = rmodel.inputconst(TYPE_ID, type_id)
        info = self.layoutbuilder.get_info(type_id)
        c_size = rmodel.inputconst(lltype.Signed, info.fixedsize)
        malloc_ptr = self.malloc_fixedsize_ptr
        c_has_finalizer = rmodel.inputconst(lltype.Bool, False)
        c_has_weakptr = rmodel.inputconst(lltype.Bool, True)
        args = [self.c_const_gc, c_type_id, c_size,
                c_has_finalizer, c_has_weakptr]

        # push and pop the current live variables *including* the argument
        # to the weakref_create operation, which must be kept alive and
        # moved if the GC needs to collect
        livevars = self.push_roots(hop, keep_current_args=True)
        v_result = hop.genop("direct_call", [malloc_ptr] + args,
                             resulttype=llmemory.GCREF)
        v_result = hop.genop("cast_opaque_ptr", [v_result],
                            resulttype=WEAKREFPTR)
        self.pop_roots(hop, livevars)
        # cast_ptr_to_adr must be done after malloc, as the GC pointer
        # might have moved just now.
        v_instance, = op.args
        v_addr = hop.genop("cast_ptr_to_adr", [v_instance],
                           resulttype=llmemory.Address)
        hop.genop("bare_setfield",
                  [v_result, rmodel.inputconst(lltype.Void, "weakptr"), v_addr])
        v_weakref = hop.genop("cast_ptr_to_weakrefptr", [v_result],
                              resulttype=llmemory.WeakRefPtr)
        hop.cast_result(v_weakref)

    def gct_weakref_deref(self, hop):
        v_wref, = hop.spaceop.args
        v_addr = hop.genop("direct_call",
                           [self.weakref_deref_ptr, v_wref],
                           resulttype=llmemory.Address)
        hop.cast_result(v_addr)

    def gct_gc_identityhash(self, hop):
        livevars = self.push_roots(hop)
        [v_ptr] = hop.spaceop.args
        v_ptr = hop.genop("cast_opaque_ptr", [v_ptr],
                          resulttype=llmemory.GCREF)
        hop.genop("direct_call",
                  [self.identityhash_ptr, self.c_const_gc, v_ptr],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_gc_id(self, hop):
        if self.id_ptr is not None:
            livevars = self.push_roots(hop)
            [v_ptr] = hop.spaceop.args
            v_ptr = hop.genop("cast_opaque_ptr", [v_ptr],
                              resulttype=llmemory.GCREF)
            hop.genop("direct_call", [self.id_ptr, self.c_const_gc, v_ptr],
                      resultvar=hop.spaceop.result)
            self.pop_roots(hop, livevars)
        else:
            hop.rename('cast_ptr_to_int')     # works nicely for non-moving GCs

    def gct_gc_obtain_free_space(self, hop):
        livevars = self.push_roots(hop)
        [v_number] = hop.spaceop.args
        hop.genop("direct_call",
                  [self.obtainfreespace_ptr, self.c_const_gc, v_number],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_gc_set_max_heap_size(self, hop):
        [v_size] = hop.spaceop.args
        hop.genop("direct_call", [self.set_max_heap_size_ptr,
                                  self.c_const_gc,
                                  v_size])

    def gct_gc_thread_prepare(self, hop):
        pass   # no effect any more

    def gct_gc_thread_run(self, hop):
        assert self.translator.config.translation.thread
        if hasattr(self.root_walker, 'thread_run_ptr'):
            livevars = self.push_roots(hop)
            hop.genop("direct_call", [self.root_walker.thread_run_ptr])
            self.pop_roots(hop, livevars)

    def gct_gc_thread_start(self, hop):
        assert self.translator.config.translation.thread
        if hasattr(self.root_walker, 'thread_start_ptr'):
            # only with asmgcc.  Note that this is actually called after
            # the first gc_thread_run() in the new thread.
            hop.genop("direct_call", [self.root_walker.thread_start_ptr])

    def gct_gc_thread_die(self, hop):
        assert self.translator.config.translation.thread
        if hasattr(self.root_walker, 'thread_die_ptr'):
            livevars = self.push_roots(hop)
            hop.genop("direct_call", [self.root_walker.thread_die_ptr])
            self.pop_roots(hop, livevars)

    def gct_gc_thread_before_fork(self, hop):
        if (self.translator.config.translation.thread
            and hasattr(self.root_walker, 'thread_before_fork_ptr')):
            hop.genop("direct_call", [self.root_walker.thread_before_fork_ptr],
                      resultvar=hop.spaceop.result)
        else:
            c_null = rmodel.inputconst(llmemory.Address, llmemory.NULL)
            hop.genop("same_as", [c_null],
                      resultvar=hop.spaceop.result)

    def gct_gc_thread_after_fork(self, hop):
        if (self.translator.config.translation.thread
            and hasattr(self.root_walker, 'thread_after_fork_ptr')):
            livevars = self.push_roots(hop)
            hop.genop("direct_call", [self.root_walker.thread_after_fork_ptr]
                                     + hop.spaceop.args)
            self.pop_roots(hop, livevars)

    def gct_gc_get_type_info_group(self, hop):
        return hop.cast_result(self.c_type_info_group)

    def gct_gc_get_rpy_roots(self, hop):
        livevars = self.push_roots(hop)
        hop.genop("direct_call",
                  [self.get_rpy_roots_ptr, self.c_const_gc],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_gc_get_rpy_referents(self, hop):
        livevars = self.push_roots(hop)
        [v_ptr] = hop.spaceop.args
        hop.genop("direct_call",
                  [self.get_rpy_referents_ptr, self.c_const_gc, v_ptr],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_gc_get_rpy_memory_usage(self, hop):
        livevars = self.push_roots(hop)
        [v_ptr] = hop.spaceop.args
        hop.genop("direct_call",
                  [self.get_rpy_memory_usage_ptr, self.c_const_gc, v_ptr],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_gc_get_rpy_type_index(self, hop):
        livevars = self.push_roots(hop)
        [v_ptr] = hop.spaceop.args
        hop.genop("direct_call",
                  [self.get_rpy_type_index_ptr, self.c_const_gc, v_ptr],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_gc_is_rpy_instance(self, hop):
        livevars = self.push_roots(hop)
        [v_ptr] = hop.spaceop.args
        hop.genop("direct_call",
                  [self.is_rpy_instance_ptr, self.c_const_gc, v_ptr],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_gc_dump_rpy_heap(self, hop):
        livevars = self.push_roots(hop)
        [v_fd] = hop.spaceop.args
        hop.genop("direct_call",
                  [self.dump_rpy_heap_ptr, self.c_const_gc, v_fd],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_gc_typeids_z(self, hop):
        livevars = self.push_roots(hop)
        hop.genop("direct_call",
                  [self.get_typeids_z_ptr, self.c_const_gc],
                  resultvar=hop.spaceop.result)
        self.pop_roots(hop, livevars)

    def gct_malloc_nonmovable_varsize(self, hop):
        TYPE = hop.spaceop.result.concretetype
        if self.gcdata.gc.can_malloc_nonmovable():
            return self.gct_malloc_varsize(hop, {'nonmovable':True})
        c = rmodel.inputconst(TYPE, lltype.nullptr(TYPE.TO))
        return hop.cast_result(c)

    def gct_malloc_nonmovable(self, hop):
        TYPE = hop.spaceop.result.concretetype
        if self.gcdata.gc.can_malloc_nonmovable():
            return self.gct_malloc(hop, {'nonmovable':True})
        c = rmodel.inputconst(TYPE, lltype.nullptr(TYPE.TO))
        return hop.cast_result(c)

    def _set_into_gc_array_part(self, op):
        if op.opname == 'setarrayitem':
            return op.args[1]
        if op.opname == 'setinteriorfield':
            for v in op.args[1:-1]:
                if v.concretetype is not lltype.Void:
                    return v
        return None

    def transform_generic_set(self, hop):
        from pypy.objspace.flow.model import Constant
        opname = hop.spaceop.opname
        v_struct = hop.spaceop.args[0]
        v_newvalue = hop.spaceop.args[-1]
        assert opname in ('setfield', 'setarrayitem', 'setinteriorfield')
        assert isinstance(v_newvalue.concretetype, lltype.Ptr)
        # XXX for some GCs the skipping if the newvalue is a constant won't be
        # ok
        if (self.write_barrier_ptr is not None
            and not isinstance(v_newvalue, Constant)
            and v_struct.concretetype.TO._gckind == "gc"
            and hop.spaceop not in self.clean_sets):
            v_newvalue = hop.genop("cast_ptr_to_adr", [v_newvalue],
                                   resulttype = llmemory.Address)
            v_structaddr = hop.genop("cast_ptr_to_adr", [v_struct],
                                     resulttype = llmemory.Address)
            if (self.write_barrier_from_array_ptr is not None and
                    self._set_into_gc_array_part(hop.spaceop) is not None):
                self.write_barrier_from_array_calls += 1
                v_index = self._set_into_gc_array_part(hop.spaceop)
                assert v_index.concretetype == lltype.Signed
                hop.genop("direct_call", [self.write_barrier_from_array_ptr,
                                          self.c_const_gc,
                                          v_newvalue,
                                          v_structaddr,
                                          v_index])
            else:
                self.write_barrier_calls += 1
                hop.genop("direct_call", [self.write_barrier_ptr,
                                          self.c_const_gc,
                                          v_newvalue,
                                          v_structaddr])
        hop.rename('bare_' + opname)

    def transform_getfield_typeptr(self, hop):
        # this would become quite a lot of operations, even if it compiles
        # to C code that is just as efficient as "obj->typeptr".  To avoid
        # that, we just generate a single custom operation instead.
        hop.genop('gc_gettypeptr_group', [hop.spaceop.args[0],
                                          self.c_type_info_group,
                                          self.c_vtinfo_skip_offset,
                                          self.c_vtableinfo],
                  resultvar = hop.spaceop.result)

    def transform_setfield_typeptr(self, hop):
        # replace such a setfield with an assertion that the typeptr is right
        # (xxx not very useful right now, so disabled)
        if 0:
            v_new = hop.spaceop.args[2]
            v_old = hop.genop('gc_gettypeptr_group', [hop.spaceop.args[0],
                                                      self.c_type_info_group,
                                                      self.c_vtinfo_skip_offset,
                                                      self.c_vtableinfo],
                              resulttype = v_new.concretetype)
            v_eq = hop.genop("ptr_eq", [v_old, v_new],
                             resulttype = lltype.Bool)
            c_errmsg = rmodel.inputconst(lltype.Void,
                                         "setfield_typeptr: wrong type")
            hop.genop('debug_assert', [v_eq, c_errmsg])

    def gct_getfield(self, hop):
        if (hop.spaceop.args[1].value == 'typeptr' and
            hop.spaceop.args[0].concretetype.TO._hints.get('typeptr') and
            self.translator.config.translation.gcremovetypeptr):
            self.transform_getfield_typeptr(hop)
        else:
            GCTransformer.gct_getfield(self, hop)

    def gct_setfield(self, hop):
        if (hop.spaceop.args[1].value == 'typeptr' and
            hop.spaceop.args[0].concretetype.TO._hints.get('typeptr') and
            self.translator.config.translation.gcremovetypeptr):
            self.transform_setfield_typeptr(hop)
        else:
            GCTransformer.gct_setfield(self, hop)

    def var_needs_set_transform(self, var):
        return var_needsgc(var)

    def push_alive_nopyobj(self, var, llops):
        pass

    def pop_alive_nopyobj(self, var, llops):
        pass

    def get_livevars_for_roots(self, hop, keep_current_args=False):
        if self.gcdata.gc.moving_gc and not keep_current_args:
            # moving GCs don't borrow, so the caller does not need to keep
            # the arguments alive
            livevars = [var for var in hop.livevars_after_op()
                            if not var_ispyobj(var)]
        else:
            livevars = hop.livevars_after_op() + hop.current_op_keeps_alive()
            livevars = [var for var in livevars if not var_ispyobj(var)]
        return livevars

    def push_roots(self, hop, keep_current_args=False):
        if self.incr_stack_ptr is None:
            return
        livevars = self.get_livevars_for_roots(hop, keep_current_args)
        self.num_pushs += len(livevars)
        if not livevars:
            return []
        c_len = rmodel.inputconst(lltype.Signed, len(livevars) )
        base_addr = hop.genop("direct_call", [self.incr_stack_ptr, c_len ],
                              resulttype=llmemory.Address)
        c_type = rmodel.inputconst(lltype.Void, llmemory.Address)
        for k,var in enumerate(livevars):
            c_k = rmodel.inputconst(lltype.Signed, k)
            v_adr = gen_cast(hop.llops, llmemory.Address, var)
            hop.genop("raw_store", [base_addr, c_type, c_k, v_adr])
        return livevars

    def pop_roots(self, hop, livevars):
        if self.decr_stack_ptr is None:
            return
        if not livevars:
            return
        c_len = rmodel.inputconst(lltype.Signed, len(livevars) )
        base_addr = hop.genop("direct_call", [self.decr_stack_ptr, c_len ],
                              resulttype=llmemory.Address)
        if self.gcdata.gc.moving_gc:
            # for moving collectors, reload the roots into the local variables
            c_type = rmodel.inputconst(lltype.Void, llmemory.Address)
            for k,var in enumerate(livevars):
                c_k = rmodel.inputconst(lltype.Signed, k)
                v_newaddr = hop.genop("raw_load", [base_addr, c_type, c_k],
                                      resulttype=llmemory.Address)
                hop.genop("gc_reload_possibly_moved", [v_newaddr, var])

    def compute_borrowed_vars(self, graph):
        # XXX temporary workaround, should be done more correctly
        if self.gcdata.gc.moving_gc:
            return lambda v: False
        return super(FrameworkGCTransformer, self).compute_borrowed_vars(graph)


class TransformerLayoutBuilder(gctypelayout.TypeLayoutBuilder):

    def __init__(self, translator, GCClass=None):
        if GCClass is None:
            from pypy.rpython.memory.gc.base import choose_gc_from_config
            GCClass, _ = choose_gc_from_config(translator.config)
        if translator.config.translation.gcremovetypeptr:
            lltype2vtable = translator.rtyper.lltype2vtable
        else:
            lltype2vtable = None
        super(TransformerLayoutBuilder, self).__init__(GCClass, lltype2vtable)

    def has_finalizer(self, TYPE):
        rtti = get_rtti(TYPE)
        return rtti is not None and getattr(rtti._obj, 'destructor_funcptr',
                                            None)

    def has_custom_trace(self, TYPE):
        rtti = get_rtti(TYPE)
        return rtti is not None and getattr(rtti._obj, 'custom_trace_funcptr',
                                            None)

    def make_finalizer_funcptr_for_type(self, TYPE):
        if not self.has_finalizer(TYPE):
            return None
        rtti = get_rtti(TYPE)
        destrptr = rtti._obj.destructor_funcptr
        DESTR_ARG = lltype.typeOf(destrptr).TO.ARGS[0]
        assert not type_contains_pyobjs(TYPE), "not implemented"
        typename = TYPE.__name__
        def ll_finalizer(addr, ignored):
            v = llmemory.cast_adr_to_ptr(addr, DESTR_ARG)
            ll_call_destructor(destrptr, v, typename)
            return llmemory.NULL
        fptr = self.transformer.annotate_finalizer(ll_finalizer,
                [llmemory.Address, llmemory.Address], llmemory.Address)
        return fptr

    def make_custom_trace_funcptr_for_type(self, TYPE):
        if not self.has_custom_trace(TYPE):
            return None
        rtti = get_rtti(TYPE)
        fptr = rtti._obj.custom_trace_funcptr
        if not hasattr(fptr._obj, 'graph'):
            ll_func = fptr._obj._callable
            fptr = self.transformer.annotate_finalizer(ll_func,
                    [llmemory.Address, llmemory.Address], llmemory.Address)
        return fptr


def gen_zero_gc_pointers(TYPE, v, llops, previous_steps=None):
    if previous_steps is None:
        previous_steps = []
    assert isinstance(TYPE, lltype.Struct)
    for name in TYPE._names:
        c_name = rmodel.inputconst(lltype.Void, name)
        FIELD = getattr(TYPE, name)
        if isinstance(FIELD, lltype.Ptr) and FIELD._needsgc():
            c_null = rmodel.inputconst(FIELD, lltype.nullptr(FIELD.TO))
            if not previous_steps:
                llops.genop('bare_setfield', [v, c_name, c_null])
            else:
                llops.genop('bare_setinteriorfield',
                            [v] + previous_steps + [c_name, c_null])
        elif isinstance(FIELD, lltype.Struct):
            gen_zero_gc_pointers(FIELD, v, llops, previous_steps + [c_name])

# ____________________________________________________________


sizeofaddr = llmemory.sizeof(llmemory.Address)


class BaseRootWalker(object):
    need_root_stack = False
    thread_setup = None

    def __init__(self, gctransformer):
        self.gcdata = gctransformer.gcdata
        self.gc = self.gcdata.gc

    def _freeze_(self):
        return True

    def setup_root_walker(self):
        if self.thread_setup is not None:
            self.thread_setup()

    def walk_roots(self, collect_stack_root,
                   collect_static_in_prebuilt_nongc,
                   collect_static_in_prebuilt_gc):
        gcdata = self.gcdata
        gc = self.gc
        if collect_static_in_prebuilt_nongc:
            addr = gcdata.static_root_start
            end = gcdata.static_root_nongcend
            while addr != end:
                result = addr.address[0]
                if gc.points_to_valid_gc_object(result):
                    collect_static_in_prebuilt_nongc(gc, result)
                addr += sizeofaddr
        if collect_static_in_prebuilt_gc:
            addr = gcdata.static_root_nongcend
            end = gcdata.static_root_end
            while addr != end:
                result = addr.address[0]
                if gc.points_to_valid_gc_object(result):
                    collect_static_in_prebuilt_gc(gc, result)
                addr += sizeofaddr
        if collect_stack_root:
            self.walk_stack_roots(collect_stack_root)     # abstract

    def need_stacklet_support(self):
        raise Exception("%s does not support stacklets" % (
            self.__class__.__name__,))

    def need_thread_support(self, gctransformer, getfn):
        raise Exception("%s does not support threads" % (
            self.__class__.__name__,))
