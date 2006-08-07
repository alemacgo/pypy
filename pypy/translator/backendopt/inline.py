import sys
from pypy.translator.simplify import join_blocks, cleanup_graph
from pypy.translator.simplify import get_graph
from pypy.translator.unsimplify import copyvar
from pypy.objspace.flow.model import Variable, Constant, Block, Link
from pypy.objspace.flow.model import SpaceOperation, c_last_exception
from pypy.objspace.flow.model import FunctionGraph
from pypy.objspace.flow.model import traverse, mkentrymap, checkgraph
from pypy.annotation import model as annmodel
from pypy.rpython.lltypesystem.lltype import Bool, typeOf, Void, Ptr
from pypy.rpython.lltypesystem.lltype import normalizeptr
from pypy.rpython import rmodel
from pypy.tool.algo import sparsemat
from pypy.translator.backendopt.support import log, split_block_with_keepalive
from pypy.translator.backendopt.support import generate_keepalive, find_backedges, find_loop_blocks
from pypy.translator.backendopt.canraise import RaiseAnalyzer

BASE_INLINE_THRESHOLD = 32.4    # just enough to inline add__Int_Int()
# and just small enough to prevend inlining of some rlist functions.

class CannotInline(Exception):
    pass


def collect_called_graphs(graph, translator):
    graphs_or_something = {}
    for block in graph.iterblocks():
        for op in block.operations:
            if op.opname == "direct_call":
                graph = get_graph(op.args[0], translator)
                if graph is not None:
                    graphs_or_something[graph] = True
                else:
                    graphs_or_something[op.args[0]] = True
            if op.opname == "indirect_call":
                graphs = op.args[-1].value
                if graphs is None:
                    graphs_or_something[op.args[0]] = True
                else:
                    for graph in graphs:
                        graphs_or_something[graph] = True
    return graphs_or_something

def iter_callsites(graph, calling_what):
    for block in graph.iterblocks():
        for i, op in enumerate(block.operations):
            if not op.opname == "direct_call":
                continue
            funcobj = op.args[0].value._obj
            graph = getattr(funcobj, 'graph', None)
            # accept a function or a graph as 'inline_func'
            if (graph is calling_what or
                getattr(funcobj, '_callable', None) is calling_what):
                yield graph, block, i

def find_callsites(graph, calling_what):
    return list(iter_callsites(graph, calling_what))

def iter_first_callsites(graph, calling_what):
    # restart the iter_callsites iterator every time, since the graph might
    # have changed
    while 1:
        iterator = iter_callsites(graph, calling_what)
        yield iterator.next()

def contains_call(graph, calling_what):
    try:
        iterator = iter_callsites(graph, calling_what)
        iterator.next()
        return True
    except StopIteration:
        return False

def inline_function(translator, inline_func, graph, lltype_to_classdef,
                    raise_analyzer):
    inliner = Inliner(translator, graph, inline_func, lltype_to_classdef,
                      raise_analyzer = raise_analyzer)
    return inliner.inline_all()

def simple_inline_function(translator, inline_func, graph):
    inliner = Inliner(translator, graph, inline_func,
                      translator.rtyper.lltype_to_classdef_mapping(),
                      raise_analyzer = RaiseAnalyzer(translator))
    return inliner.inline_all()


def _find_exception_type(block):
    #XXX slightly brittle: find the exception type for simple cases
    #(e.g. if you do only raise XXXError) by doing pattern matching
    currvar = block.exits[0].args[1]
    ops = block.operations
    i = len(ops)-1
    while True:
        if isinstance(currvar, Constant):
            return typeOf(normalizeptr(currvar.value)), block.exits[0]
        if i < 0:
            return None, None
        op = ops[i]
        i -= 1
        if op.opname in ("same_as", "cast_pointer") and op.result is currvar:
            currvar = op.args[0]
        elif op.opname == "malloc" and op.result is currvar:
            return Ptr(op.args[0].value), block.exits[0]

def does_raise_directly(graph, raise_analyzer):
    """ this function checks, whether graph contains operations which can raise
    and which are not exception guarded """
    for block in graph.iterblocks():
        if block is graph.exceptblock:
            return True      # the except block is reachable
        if block.exitswitch == c_last_exception:
            consider_ops_to = -1
        else:
            consider_ops_to = len(block.operations)
        for op in block.operations[:consider_ops_to]:
            if raise_analyzer.can_raise(op):
                return True
    return False

def any_call_to_raising_graphs(from_graph, translator, raise_analyzer):
    for graph in collect_called_graphs(from_graph, translator):
        if not isinstance(graph, FunctionGraph):
            return True     # conservatively
        if does_raise_directly(graph, raise_analyzer):
            return True
    return False

class BaseInliner(object):
    def __init__(self, translator, graph, lltype_to_classdef, 
                 inline_guarded_calls=False,
                 inline_guarded_calls_no_matter_what=False, raise_analyzer=None):
        self.translator = translator
        self.graph = graph
        self.inline_guarded_calls = inline_guarded_calls
        # if this argument is set, the inliner will happily produce wrong code!
        # it is used by the exception transformation
        self.inline_guarded_calls_no_matter_what = inline_guarded_calls_no_matter_what
        assert raise_analyzer is not None
        self.raise_analyzer = raise_analyzer
        self.lltype_to_classdef = lltype_to_classdef

    def inline_all(self):
        count = 0
        non_recursive = {}
        while self.block_to_index:
            block, d = self.block_to_index.popitem()
            index_operation, subgraph = d.popitem()
            if d:
                self.block_to_index[block] = d
            if subgraph not in non_recursive and contains_call(subgraph, subgraph):
                raise CannotInline("inlining a recursive function")
            else:
                non_recursive[subgraph] = True
            operation = block.operations[index_operation]
            self.inline_once(block, index_operation)
            count += 1
        self.cleanup()
        return count

    def inline_once(self, block, index_operation):
        self.varmap = {}
        self._copied_blocks = {}
        self.op = block.operations[index_operation]
        self.graph_to_inline = self.op.args[0].value._obj.graph
        self.exception_guarded = False
        if (block.exitswitch == c_last_exception and
            index_operation == len(block.operations) - 1):
            self.exception_guarded = True
            if self.inline_guarded_calls:
                if (not self.inline_guarded_calls_no_matter_what and 
                    does_raise_directly(self.graph_to_inline, self.raise_analyzer)):
                    raise CannotInline("can't inline because the call is exception guarded")
            elif any_call_to_raising_graphs(self.graph_to_inline,
                                            self.translator, self.raise_analyzer):
                raise CannotInline("can't handle exceptions")
        self._passon_vars = {}
        self.entrymap = mkentrymap(self.graph_to_inline)
        self.do_inline(block, index_operation)

    def search_for_calls(self, block):
        d = {}
        for i, op in enumerate(block.operations):
            if not op.opname == "direct_call":
                continue
            funcobj = op.args[0].value._obj
            graph = getattr(funcobj, 'graph', None)
            # accept a function or a graph as 'inline_func'
            if (graph is self.inline_func or
                getattr(funcobj, '_callable', None) is self.inline_func):
                d[i] = graph
        if d:
            self.block_to_index[block] = d
        else:
            try:
                del self.block_to_index[block]
            except KeyError:
                pass

    def get_new_name(self, var):
        if var is None:
            return None
        if isinstance(var, Constant):
            return var
        if var not in self.varmap:
            self.varmap[var] = copyvar(None, var)
        return self.varmap[var]
        
    def passon_vars(self, cache_key):
        if cache_key in self._passon_vars:
            return self._passon_vars[cache_key]
        result = [copyvar(None, var)
                      for var in self.original_passon_vars]
        self._passon_vars[cache_key] = result
        return result
        
    def copy_operation(self, op):
        args = [self.get_new_name(arg) for arg in op.args]
        result = SpaceOperation(op.opname, args, self.get_new_name(op.result))
        return result

    def copy_block(self, block):
        if block in self._copied_blocks:
            return self._copied_blocks[block]
        args = ([self.get_new_name(var) for var in block.inputargs] +
                self.passon_vars(block))
        newblock = Block(args)
        self._copied_blocks[block] = newblock
        newblock.operations = [self.copy_operation(op) for op in block.operations]
        newblock.exits = [self.copy_link(link, block) for link in block.exits]
        newblock.exitswitch = self.get_new_name(block.exitswitch)
        newblock.exc_handler = block.exc_handler
        self.search_for_calls(newblock)
        return newblock

    def copy_link(self, link, prevblock):
        newargs = [self.get_new_name(a) for a in link.args] + self.passon_vars(prevblock)
        newlink = Link(newargs, self.copy_block(link.target), link.exitcase)
        newlink.prevblock = self.copy_block(link.prevblock)
        newlink.last_exception = self.get_new_name(link.last_exception)
        newlink.last_exc_value = self.get_new_name(link.last_exc_value)
        if hasattr(link, 'llexitcase'):
            newlink.llexitcase = link.llexitcase
        return newlink
        

    def find_args_in_exceptional_case(self, link, block, etype, evalue, afterblock, passon_vars):
        linkargs = []
        for arg in link.args:
            if arg == link.last_exception:
                linkargs.append(etype)
            elif arg == link.last_exc_value:
                linkargs.append(evalue)
            elif isinstance(arg, Constant):
                linkargs.append(arg)
            else:
                index = afterblock.inputargs.index(arg)
                linkargs.append(passon_vars[index - 1])
        return linkargs

    def rewire_returnblock(self, afterblock):
        copiedreturnblock = self.copy_block(self.graph_to_inline.returnblock)
        linkargs = ([copiedreturnblock.inputargs[0]] +
                    self.passon_vars(self.graph_to_inline.returnblock))
        linkfrominlined = Link(linkargs, afterblock)
        linkfrominlined.prevblock = copiedreturnblock
        copiedreturnblock.exitswitch = None
        copiedreturnblock.exits = [linkfrominlined]
        assert copiedreturnblock.exits[0].target == afterblock
       
    def rewire_exceptblock(self, afterblock):
        #let links to exceptblock of the graph to inline go to graphs exceptblock
        copiedexceptblock = self.copy_block(self.graph_to_inline.exceptblock)
        if not self.exception_guarded:
            self.rewire_exceptblock_no_guard(afterblock, copiedexceptblock)
        else:
            # first try to match exceptions using a very simple heuristic
            self.rewire_exceptblock_with_guard(afterblock, copiedexceptblock)
            # generate blocks that do generic matching for cases when the
            # heuristic did not work
#            self.translator.view()
            self.generic_exception_matching(afterblock, copiedexceptblock)

    def rewire_exceptblock_no_guard(self, afterblock, copiedexceptblock):
         # find all copied links that go to copiedexceptblock
        for link in self.entrymap[self.graph_to_inline.exceptblock]:
            copiedblock = self.copy_block(link.prevblock)
            for copiedlink in copiedblock.exits:
                if copiedlink.target is copiedexceptblock:
                    copiedlink.args = copiedlink.args[:2]
                    copiedlink.target = self.graph.exceptblock
                    for a1, a2 in zip(copiedlink.args,
                                      self.graph.exceptblock.inputargs):
                        if hasattr(a2, 'concretetype'):
                            assert a1.concretetype == a2.concretetype
                        else:
                            # if self.graph.exceptblock was never used before
                            a2.concretetype = a1.concretetype
    
    def rewire_exceptblock_with_guard(self, afterblock, copiedexceptblock):
        # this rewiring does not always succeed. in the cases where it doesn't
        # there will be generic code inserted
        from pypy.rpython.lltypesystem import rclass
        exc_match = self.translator.rtyper.getexceptiondata().fn_exception_match
        for link in self.entrymap[self.graph_to_inline.exceptblock]:
            copiedblock = self.copy_block(link.prevblock)
            VALUE, copiedlink = _find_exception_type(copiedblock)
            #print copiedblock.operations
            if VALUE is None or VALUE not in self.lltype_to_classdef:
                continue
            classdef = self.lltype_to_classdef[VALUE]
            rtyper = self.translator.rtyper
            classrepr = rclass.getclassrepr(rtyper, classdef)
            vtable = classrepr.getruntime()
            var_etype = copiedlink.args[0]
            var_evalue = copiedlink.args[1]
            for exceptionlink in afterblock.exits[1:]:
                if exc_match(vtable, exceptionlink.llexitcase):
                    passon_vars = self.passon_vars(link.prevblock)
                    copiedblock.operations += generate_keepalive(passon_vars)
                    copiedlink.target = exceptionlink.target
                    linkargs = self.find_args_in_exceptional_case(
                        exceptionlink, link.prevblock, var_etype, var_evalue, afterblock, passon_vars)
                    copiedlink.args = linkargs
                    break

    def generic_exception_matching(self, afterblock, copiedexceptblock):
        #XXXXX don't look: insert blocks that do exception matching
        #for the cases where direct matching did not work
        exc_match = Constant(
            self.translator.rtyper.getexceptiondata().fn_exception_match)
        exc_match.concretetype = typeOf(exc_match.value)
        blocks = []
        for i, link in enumerate(afterblock.exits[1:]):
            etype = copyvar(None, copiedexceptblock.inputargs[0])
            evalue = copyvar(None, copiedexceptblock.inputargs[1])
            passon_vars = self.passon_vars(i)
            block = Block([etype, evalue] + passon_vars)
            res = Variable()
            res.concretetype = Bool
            cexitcase = Constant(link.llexitcase)
            cexitcase.concretetype = typeOf(cexitcase.value)
            args = [exc_match, etype, cexitcase]
            block.operations.append(SpaceOperation("direct_call", args, res))
            block.exitswitch = res
            linkargs = self.find_args_in_exceptional_case(link, link.target,
                                                          etype, evalue, afterblock,
                                                          passon_vars)
            l = Link(linkargs, link.target)
            l.prevblock = block
            l.exitcase = True
            l.llexitcase = True
            block.exits.append(l)
            if i > 0:
                l = Link(blocks[-1].inputargs, block)
                l.prevblock = blocks[-1]
                l.exitcase = False
                l.llexitcase = False
                blocks[-1].exits.insert(0, l)
            blocks.append(block)
        blocks[-1].exits = blocks[-1].exits[:1]
        blocks[-1].operations = []
        blocks[-1].exitswitch = None
        blocks[-1].exits[0].exitcase = None
        del blocks[-1].exits[0].llexitcase
        linkargs = copiedexceptblock.inputargs
        copiedexceptblock.closeblock(Link(linkargs, blocks[0]))
        copiedexceptblock.operations += generate_keepalive(linkargs)

      
    def do_inline(self, block, index_operation):
        splitlink = split_block_with_keepalive(block, index_operation)
        afterblock = splitlink.target
        # these variables have to be passed along all the links in the inlined
        # graph because the original function needs them in the blocks after
        # the inlined function
        # for every inserted block we need a new copy of these variables,
        # this copy is created with the method passon_vars
        self.original_passon_vars = [arg for arg in block.exits[0].args
                                         if isinstance(arg, Variable)]
        n = 0
        while afterblock.operations[n].opname == 'keepalive':
            n += 1
        assert afterblock.operations[n].opname == self.op.opname
        self.op = afterblock.operations.pop(n)
        #vars that need to be passed through the blocks of the inlined function
        linktoinlined = splitlink 
        copiedstartblock = self.copy_block(self.graph_to_inline.startblock)
        copiedstartblock.isstartblock = False
        #find args passed to startblock of inlined function
        passon_args = []
        for arg in self.op.args[1:]:
            if isinstance(arg, Constant):
                passon_args.append(arg)
            else:
                index = afterblock.inputargs.index(arg)
                passon_args.append(linktoinlined.args[index])
        passon_args += self.original_passon_vars
        #rewire blocks
        linktoinlined.target = copiedstartblock
        linktoinlined.args = passon_args
        afterblock.inputargs = [self.op.result] + afterblock.inputargs
        if self.graph_to_inline.returnblock in self.entrymap:
            self.rewire_returnblock(afterblock) 
        if self.graph_to_inline.exceptblock in self.entrymap:
            self.rewire_exceptblock(afterblock)
        if self.exception_guarded:
            assert afterblock.exits[0].exitcase is None
            afterblock.exits = [afterblock.exits[0]]
            afterblock.exitswitch = None
        self.search_for_calls(afterblock)
        self.search_for_calls(block)

    def cleanup(self):
        """ cleaning up -- makes sense to be done after inlining, because the
        inliner inserted quite some empty blocks and blocks that can be
        joined. """
        cleanup_graph(self.graph)


class Inliner(BaseInliner):
    def __init__(self, translator, graph, inline_func, lltype_to_classdef, inline_guarded_calls=False,
                 inline_guarded_calls_no_matter_what=False, raise_analyzer=None):
        BaseInliner.__init__(self, translator, graph, lltype_to_classdef,
                             inline_guarded_calls,
                             inline_guarded_calls_no_matter_what,
                             raise_analyzer)
        self.inline_func = inline_func
        # to simplify exception matching
        join_blocks(graph)
        # find callsites *after* joining blocks...
        callsites = find_callsites(graph, inline_func)
        self.block_to_index = {}
        for g, block, i in callsites:
            self.block_to_index.setdefault(block, {})[i] = g

class OneShotInliner(BaseInliner):
    def search_for_calls(self, block):
        pass


# ____________________________________________________________
#
# Automatic inlining

OP_WEIGHTS = {'same_as': 0,
              'cast_pointer': 0,
              'keepalive': 0,
              'malloc': 2,
              'yield_current_frame_to_caller': sys.maxint, # XXX bit extreme
              'resume_point': sys.maxint, # XXX bit extreme
              }

def block_weight(block, weights=OP_WEIGHTS):
    total = 0
    for op in block.operations:
        if op.opname == "direct_call":
            total += 1.5 + len(op.args) / 2
        elif op.opname == "indirect_call":
            total += 2 + len(op.args) / 2
        total += weights.get(op.opname, 1)
    if block.exitswitch is not None:
        total += 1
    return total


def measure_median_execution_cost(graph):
    blocks = []
    blockmap = {}
    for block in graph.iterblocks():
        blockmap[block] = len(blocks)
        blocks.append(block)
    loops = find_loop_blocks(graph)
    M = sparsemat.SparseMatrix(len(blocks))
    vector = []
    for i, block in enumerate(blocks):
        vector.append(block_weight(block))
        M[i, i] = 1
        if block.exits:
            if block not in loops:
                current_loop_start = None
            else:
                current_loop_start = loops[block]
            loop_exits = []
            for link in block.exits:
                if (link.target in loops and
                    loops[link.target] is current_loop_start):
                    loop_exits.append(link)
            if len(loop_exits) and len(loop_exits) < len(block.exits):
                f = 0.3 / (len(block.exits) - len(loop_exits))
                b = 0.7 / len(loop_exits)
            else:
                b = f = 1.0 / len(block.exits)
            for link in block.exits:
                if (link.target in loops and
                    loops[link.target] is current_loop_start):
                    M[i, blockmap[link.target]] -= b
                else:
                    M[i, blockmap[link.target]] -= f
    try:
        Solution = M.solve(vector)
    except ValueError:
        return sys.maxint
    else:
        res = Solution[blockmap[graph.startblock]]
        assert res >= 0
        return res

def static_instruction_count(graph):
    count = 0
    for block in graph.iterblocks():
        count += block_weight(block)
    return count

def inlining_heuristic(graph):
    # XXX ponderation factors?
    return (0.9999 * measure_median_execution_cost(graph) +
            static_instruction_count(graph))


def inlinable_static_callers(graphs):
    result = []
    for parentgraph in graphs:
        for block in parentgraph.iterblocks():
            for op in block.operations:
                if op.opname == "direct_call":
                    funcobj = op.args[0].value._obj
                    graph = getattr(funcobj, 'graph', None)
                    if graph is not None:
                        if getattr(getattr(funcobj, '_callable', None),
                                   'suggested_primitive', False):
                            continue
                        if getattr(getattr(funcobj, '_callable', None),
                                   'dont_inline', False):
                            continue
                        result.append((parentgraph, graph))
    return result


def auto_inlining(translator, multiplier=1, callgraph=None,
                  threshold=BASE_INLINE_THRESHOLD):
    from heapq import heappush, heappop, heapreplace, heapify
    threshold = threshold * multiplier
    callers = {}     # {graph: {graphs-that-call-it}}
    callees = {}     # {graph: {graphs-that-it-calls}}
    if callgraph is None:
        callgraph = inlinable_static_callers(translator.graphs)
    for graph1, graph2 in callgraph:
        callers.setdefault(graph2, {})[graph1] = True
        callees.setdefault(graph1, {})[graph2] = True
    # the -len(callers) change is OK
    heap = [(0.0, -len(callers[graph]), graph) for graph in callers]
    valid_weight = {}
    couldnt_inline = {}
    lltype_to_classdef = translator.rtyper.lltype_to_classdef_mapping()
    raise_analyzer = RaiseAnalyzer(translator)
    count = 0

    while heap:
        weight, _, graph = heap[0]
        if not valid_weight.get(graph):
            weight = inlining_heuristic(graph)
            #print '  + cost %7.2f %50s' % (weight, graph.name)
            heapreplace(heap, (weight, -len(callers[graph]), graph))
            valid_weight[graph] = True
            continue

        if weight >= threshold:
            # finished... unless some graphs not in valid_weight would now
            # have a weight below the threshold.  Re-insert such graphs
            # at the start of the heap
            finished = True
            for i in range(len(heap)):
                graph = heap[i][2]
                if not valid_weight.get(graph):
                    heap[i] = (0.0, heap[i][1], graph)
                    finished = False
            if finished:
                break
            else:
                heapify(heap)
                continue

        heappop(heap)
        if callers[graph]:
            log.inlining('%7.2f %50s' % (weight, graph.name))
        for parentgraph in callers[graph]:
            if parentgraph == graph:
                continue
            try:
                res = bool(inline_function(translator, graph, parentgraph,
                                           lltype_to_classdef, raise_analyzer))
            except CannotInline:
                couldnt_inline[graph] = True
                res = CannotInline
            if res is True:
                count += 1
                # the parentgraph should now contain all calls that were
                # done by 'graph'
                for graph2 in callees.get(graph, {}):
                    callees[parentgraph][graph2] = True
                    callers[graph2][parentgraph] = True
                if parentgraph in couldnt_inline:
                    # the parentgraph was previously uninlinable, but it has
                    # been modified.  Maybe now we can inline it into further
                    # parents?
                    del couldnt_inline[parentgraph]
                    heappush(heap, (0.0, -len(callers[parentgraph]), parentgraph))
                valid_weight[parentgraph] = False
    return count
