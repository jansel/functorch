import torch
from torch.fx.passes.infra.partitioner import CapabilityBasedPartitioner
from torch.fx.passes.backends.nvfuser.operator_support import NvFuserOperatorSupport
from torch.fx.passes.tools_common import legalize_graph
import torch.fx as fx
import operator
import math
import copy
from functorch.compile import draw_graph

from .utilities import _size_of, draw_nx_graph


num_group_remat = 0  # used for analytical purpose
memory_reduced = 0
num_node_pairs = 0
# no_weight_nodes = {}


    

aten = torch.ops.aten

pointwise_ops = [aten.add, aten.sub, aten.div, aten.atan2, aten.mul, aten.max, aten.min, aten.pow, aten.remainder, aten.fmod, aten.__and__, aten.__or__, aten.__xor__, aten.__lshift__, aten.__rshift__, aten.eq, aten.ne, aten.ge, aten.gt, aten.le, aten.lt, aten.abs, aten.bitwise_not, aten.ceil, aten.floor, aten.frac, aten.neg, aten.relu, aten.round, aten.silu, aten.trunc, aten.log, aten.log10, aten.log1p, aten.log2, aten.lgamma, aten.exp, aten.expm1, aten.erf, aten.erfc, aten.cos, aten.acos, aten.cosh, aten.sin, aten.asin, aten.sinh, aten.tan, aten.atan, aten.tanh, aten.atanh, aten.sqrt, aten.rsqrt,  aten.reciprocal, aten.sigmoid, aten.softplus, aten.threshold, aten.threshold_backward, aten.clamp, aten.where, aten.lerp, aten.addcmul, aten.gelu, aten.gelu_backward]  # noqa: E501
misc_ops = [aten.to, aten.type_as, operator.getitem]
reduction_ops = [aten.softmax, aten._softmax, aten._softmax_backward_data, aten.sum, aten.mean, aten._grad_sum_to_size, aten.sum_to_size, aten.amax]  # noqa: E501


norm_ops = [aten.instance_norm, aten._batch_norm_impl_index, aten.native_batch_norm, aten.batch_norm, aten._batch_norm_impl_index_backward, aten.native_layer_norm, aten.layer_norm, aten.native_layer_norm_backward]  # noqa: E501
view_ops = [aten.squeeze, aten.unsqueeze]
random_ops = [aten.native_dropout, aten.rand_like, aten.randn_like]
compute_intensive_ops = [aten.mm, aten.convolution, aten.convolution_backward, aten.bmm, aten.addmm, aten.upsample_bilinear2d]  # noqa: E501
unrecomputable_ops = random_ops + compute_intensive_ops + norm_ops

recomputable_ops = set(
    pointwise_ops
    + misc_ops
    + reduction_ops
    + view_ops
)
fusible_ops = recomputable_ops | set(random_ops)

AGGRESSIVE_RECOMPUTATION = False

def ban_recomputation(node):
    if AGGRESSIVE_RECOMPUTATION:
        return (node.op == 'call_function' and node.target in unrecomputable_ops)
    else:
        if node.op != 'call_function':
            return False
        if node.target not in recomputable_ops:
            return True
        # If the output of the reduction is 4x smaller (arbitrary choice),
        # then we don't allow recomputation.
        if node.target in reduction_ops:
            input_tensors_size = sum(_size_of(i.meta['tensor_meta']) for i in node.args if isinstance(i, fx.Node))
            output_size = _size_of(node.meta['tensor_meta'])
            return (output_size * 4 < input_tensors_size)
        return False


def is_fused_node(node):
    return node.op == "call_module" and "fused_" in node.target

def has_remat_node(node, fused_graph):
    module = getattr(fused_graph, node.name)
    try_remat = False
    for node in module.graph.nodes:
        if node.target != operator.getitem and node.op == "call_function" and not ban_recomputation(node):
            try_remat = True
            break
    return try_remat

def try_remat(node, fused_graph):
    return is_fused_node(node) and has_remat_node(node, fused_graph)

def get_users(node):
    # get the users of a node in fused graph
    # the user might use the output of node through getitem
    users = set()
    for user_node in node.users:
        if user_node.target == operator.getitem:  # TODO: any other possible skips?
            users = users.union(set(user_node.users.keys()))
        elif user_node.op != 'output':
            users.add(user_node)
    return users


def get_fused_node_pairs(fused_graph):
    # get pairs of fused node that are (parent, children) relationship in graph
    # the two (parent, children) nodes might have an getitem node between them
    fused_node_pairs = []
    for node in fused_graph.graph.nodes:
        if(try_remat(node, fused_graph)):
            users = get_users(node)
            pairs = [(node, user_node) for user_node in users if (try_remat(user_node, fused_graph))]
            fused_node_pairs.extend(pairs)
    return fused_node_pairs



def get_weight(node):
    weight = 0
    if 'tensor_meta' in node.meta:
        weight = _size_of(node.meta['tensor_meta'])
    return weight


def get_name_to_args_map(node_orig, gm):
    placeholder_map = {}  # map from placeholder name in module_origin.graph to node_pair[0].args
    loc = 0 
    for node in gm.graph.nodes:
        if node.op == "placeholder":
            placeholder_map[node.name] = node_orig.args[loc]
            loc += 1
    return placeholder_map 

def get_nx_node_name(node_name):
    if node_name.endswith("_in"):
        return node_name[:-3]
    elif node_name.endswith("_out"):
        return node_name[:-4]
    raise Exception("node name is not _in or _out, "+ node_name)


def get_cut_nodes_from_partition(partition, nx_graph):
    reachable, non_reachable = partition
    cutset = set()
    for u, nbrs in ((n, nx_graph[n]) for n in reachable):
        cutset.update((u, v) for v in nbrs if v in non_reachable)

    cut_nodes = set()
    for node_in, node_out in cutset:
        # assert node_in[:-3] == node_out[:-4]
        # node_name = node_in[:-3]
        # cut_nodes.add(node_name)
        cut_nodes.add(get_nx_node_name(node_in))
    return cut_nodes

def order_topologically(nodes, gm):
    node_order_dict = {}
    rank = 0
    for n in gm.graph.nodes:
        node_order_dict[n.name] = rank
        rank += 1
    
    nodes = sorted(nodes, key=lambda x: node_order_dict[x])
    return nodes

def get_output_node_args(node):
    if type(node.args[0]) is not tuple: # TODO: test a single output
        return node.args
    return node.args[0]


def copy_nodes(node_pair, fused_graph, name_to_node, partition, cut_nodes):
    """
    copy nodes in the non_reachable partition to module of node_pair[1]

    name_to_node is a mapping from name to nodes in fused graph
    """
    reachable, non_reachable = partition
    module_origin = getattr(fused_graph, node_pair[0].name)
    module_dest = getattr(fused_graph, node_pair[1].name)

    # print("============")
    # module_origin.graph.eliminate_dead_code()
    # print(module_origin.graph)
    # print(module_dest.graph)

    dest_placeholder_map = get_name_to_args_map(node_pair[1], module_dest)
    origin_placeholder_map = get_name_to_args_map(node_pair[0], module_origin)

    name_to_node_origin = {node.name:node for node in module_origin.graph.nodes}
    name_to_node_dest = {node.name:node for node in module_dest.graph.nodes}

    # add outputs of origin_module to dest_placheolder_map
    module_origin_new_outputs = {name_to_node_origin[name] for name in cut_nodes}
    for node in module_origin.graph.nodes:
        if node.op == "output":
            old_args = get_output_node_args(node)
            loc = 0
            for user in node_pair[0].users: #can only do this for getitem users. might have a single add node that have two users
                if user.target != operator.getitem:
                    break
                if isinstance(old_args[loc], torch.fx.node.Node):
                    user_name = old_args[loc].name
                    dest_placeholder_map[user_name] = user # add new arg to dest placeholder map
                loc += 1
            module_origin_new_outputs = list(module_origin_new_outputs.difference(set(old_args)))
            
            # need to change the user to use getitem if origin only has 1 output but now has more
            if(len(old_args)==1 and len(module_origin_new_outputs) > 0):
                with fused_graph.graph.inserting_after(node_pair[0]):
                    new_node = fused_graph.graph.call_function(operator.getitem, args=(node_pair[0], 0,))
                node_pair[0].replace_all_uses_with(new_node)
                new_node.args=(node_pair[0], 0,)
                name_to_node[node_pair[0].name] = new_node # add new arg to dest placeholder map
                dest_placeholder_map[old_args[0].name]=new_node

            if len(module_origin_new_outputs) > 0:  # need to add new ouputs to module_origin and new inputs to module_dest
                with fused_graph.graph.inserting_after(node_pair[0]):
                    for i in range(len(module_origin_new_outputs )):
                        new_node = fused_graph.graph.call_function(operator.getitem, args=(node_pair[0], i + len(old_args),))
                        dest_placeholder_map[module_origin_new_outputs[i].name] = new_node # add new arg to dest placeholder map
                new_args = list(old_args) + module_origin_new_outputs
                module_origin.graph.erase_node(node)
                module_origin.graph.output(new_args[0] if len(new_args) == 1 else tuple(new_args)) # TODO: test a single output
            break
    

    module_origin.recompile()
    fused_graph.recompile()

    node_to_copy = set()
    for node_name in non_reachable:
        if node_name == "sink":
            continue
        node_name = get_nx_node_name(node_name)
        node_to_copy.add(node_name)
    node_to_copy = node_to_copy.difference(cut_nodes)  # cut nodes are handeled separately as placeholders

    first_node_dest = None
    for node in module_dest.graph.nodes:
        first_node_dest = node
        break



    env = {}  # map from node in origin to node in dest
    # new placeholders, TODO: check if there are existing placeholders
    for node_name in cut_nodes:
        node = name_to_node_origin[node_name]
        if node_name in name_to_node_dest:
            # already has a placeholder for it in dest
            env[node] = name_to_node_dest[node_name]
            continue  
        with module_dest.graph.inserting_before(first_node_dest):
            new_node = module_dest.graph.placeholder(node.name, type_expr=node.type)
            new_node.meta = copy.copy(node.meta)
            env[node] = new_node

    # copy internal nodes
    node_to_copy = order_topologically(node_to_copy , module_origin)
    for node_name in node_to_copy:
        node = name_to_node_origin[node_name]
        with module_dest.graph.inserting_before(first_node_dest):
            new_node = module_dest.graph.node_copy(node, lambda x: env[x])
            new_node.name = node.name # use the same name such that node can be referenced back to original graph
            env[node] = new_node
            # change the args of nodes in dest to use the new node
            if node.name in name_to_node_dest:
                name_to_node_dest[node.name].replace_all_uses_with(new_node)

    # erase old placeholder nodes and record current active placeholders
    active_placeholders = []
    for node in module_dest.graph.nodes:
        if node.op == "placeholder":
            if len(node.users) == 0:
                module_dest.graph.erase_node(node)
            else:
                active_placeholders.append(node.name)

    legalize_graph(module_dest)
    module_dest.graph.eliminate_dead_code()
    module_dest.graph.lint()

    # change the args of dest node in fused_graph
    # use origin_placeholder_map because the active place_holders 
    # might be in another module, and thus need get_item
    # breakpoint()
    # for node in fused_graph.graph.nodes:
    #     if(node.name == module_dest.name):
    node = node_pair[1]  # dest node
    new_args = []
    for name in active_placeholders:
        if name in name_to_node: # name is a node in fused graph
            new_args.append(name_to_node[name])
        elif name in origin_placeholder_map: # name is a placeholder in origin's module
            new_args.append(origin_placeholder_map[name])
        else: # name is a placeholder in dest's module or a newly added input
            new_args.append(dest_placeholder_map[name])
    node.args = tuple(new_args)
            # break
    # breakpoint()
    fused_graph.recompile()
    # legalize_graph(fused_graph)  # TODO:why this hang sometimes?
    fused_graph.graph.eliminate_dead_code()
    fused_graph.graph.lint()
    module_dest.recompile()

    # remove the unsed output to write less
    # Use 0 instead of remove entirely because getitem will index into the outputs
    # Assumption: each module node has a single output node
    # Need to do this after fused_graph.graph.eliminate_dead_code() such that
    # extra getitem operators are removed.
    used_inds = set()

    # need to modify the node in fused_graph, not the node passed in pairs
    for node in fused_graph.graph.nodes:
        if(node.name == module_origin.name):
            for node_user in node.users:
                if node_user.target == operator.getitem:
                    used_inds.add(node_user.args[1])
            break

    for node in module_origin.graph.nodes:
        if node.op == "output":
            if (len(used_inds) == 0 and type(node.args[0] is not tuple)): # only has a single output
                break
            new_args = []
            for i in range(len(node.args[0])):
                if i in used_inds:
                    new_args.append(node.args[0][i]) # still useful
                else:
                    new_args.append(None) # no need to write out
            node.args = tuple([tuple(new_args),])
            break
    module_origin.recompile() 
    fused_graph.recompile()
    # print("============")
    # print(module_origin.graph)
    # print(module_dest.graph)
    # # print(fused_graph.graph)
    # exit(0)


def find_min_cut(node_pair, node_users_map, fused_graph):
    """
        The mincut value is the cost of reading/writing between the two fusion groups
    """

    try:
        import networkx as nx 
    except ImportError:
        raise RuntimeError("Need networkx installed to perform smart recomputation heuristics")
    nx_graph = nx.DiGraph()
    node_origin = node_pair[0]
    node_dest = node_pair[1]
    module_origin = getattr(fused_graph, node_origin.name)
    module_dest = getattr(fused_graph, node_dest.name)

    dest_placeholder_names = set(node.name for node in module_dest.graph.nodes if node.op == "placeholder")
    # used to check if a node has users in dest. The user node in the original graph has the same name as the call_func nodes in dest.
    dest_node_names = set(node.name for node in module_dest.graph.nodes if node.op != "placeholder" and node.op != "output")
    orig_node_names = set(node.name for node in module_origin.graph.nodes if node.op != "placeholder" and node.op != "output")


    # track the users of each node in traced_graph
    getitem_users = {}
    for node in module_origin.graph.nodes:
        if node.op == "output":
            output_args = get_output_node_args(node)
    loc = 0
    for user in node_origin.users: #can only do this for getitem users. might have a single add node that have two users
        if user.target != operator.getitem:
            break
        if isinstance(output_args[loc], torch.fx.node.Node):
            user_name = output_args[loc].name
            getitem_users[user_name] = user.name # add new arg to dest placeholder map
        loc += 1

    def get_capacity(node):
        # if rematerialize an internal node, need to read and write
        # might not need to add the write cost, because it might be read by other
        # might not need to add the read cost, if already reading it - no need the cost
        # TODO: test case for both
        user_names_set = set({n.name for n in node_users_map[node.name]})
        user_names_outside_set = user_names_set.difference(orig_node_names)
        write_cost = 0 # cost for both read and write because only dest_module is using it
        if weight and user_names_outside_set.issubset(set(dest_node_names)):
            write_cost = weight  
        
        read_cost = weight

        capacity = write_cost+read_cost
        return capacity


    for node in module_origin.graph.nodes:
        if node.op == 'output':
            continue

        weight = get_weight(node)

        # breakpoint()
        if ban_recomputation(node):
            nx_graph.add_edge("source",  node.name+"_out", capacity=math.inf)   

        # some ops like cuda_batch_norm return tuples, and they cannot be returned as output
        # because torch.jit.script does not accept 
        # need to return getitem, these getitems might already in the graph
        # neeed to change the capacity between _in and _out of these ndoes to inf
        for user in node.users:
            if user.target == operator.getitem:
                weight = math.inf
            break

        if node.op == 'placeholder':
            capacity=weight
            nx_graph.add_edge("source", node.name+"_in", capacity=math.inf)   
        elif node.op ==  'call_function':
            capacity = get_capacity(node)

        
        if (node.name in dest_placeholder_names or 
          (node.name in getitem_users and getitem_users[node.name] in dest_placeholder_names)): # usage over getitem in fused graph
            nx_graph.add_edge(node.name+"_out", 'sink', capacity=capacity)
        
        nx_graph.add_edge(node.name+"_in", node.name+"_out", capacity=capacity)
        for user in node.users:
            if user.op != "output":
                nx_graph.add_edge(node.name+"_out", user.name+"_in", capacity=math.inf)

    # draw_nx_graph(nx_graph)
    cut_value, partition = nx.minimum_cut(nx_graph, "source", "sink")

    cut_at_sink = 0
    for e in nx_graph.edges.data():
        if e[1] == "sink":
            cut_at_sink += e[2]["capacity"]

    # print(cut_at_sink, cut_value)
    global memory_reduced 
    memory_reduced = cut_at_sink - cut_value
    # for edge in nx_graph.edges.data():
    #     print(edge)
    # print(cut_value, partition)
    # breakpoint()
    # if(memory_reduced > 0):
    #     breakpoint()
    cut_nodes = get_cut_nodes_from_partition(partition, nx_graph)
    # print(cut_nodes)
    return partition, cut_nodes


def check_remat(partition):
    _, non_reachable = partition
    return non_reachable != {"sink"}

def get_fused_graph(traced_graph):
    supported_ops = NvFuserOperatorSupport()
    partitioner = CapabilityBasedPartitioner(traced_graph, supported_ops)
    fused_graph = partitioner.partition_and_fuse()
    return fused_graph


def rematerialize_fused_graph(fused_graph, node_users_map):
    global num_group_remat, num_node_pairs
    name_to_node = {node.name:node for node in fused_graph.graph.nodes}

    fused_node_pairs = get_fused_node_pairs(fused_graph)
    num_node_pairs = len(fused_node_pairs)
    for node_pair in fused_node_pairs:
        partition, cut_nodes = find_min_cut(node_pair, node_users_map, fused_graph)
        if check_remat(partition):
            num_group_remat += 1
            copy_nodes(node_pair, fused_graph, name_to_node, partition, cut_nodes)
    return fused_graph


def rematerialize(traced_graph):
    traced_graph.graph.eliminate_dead_code()
    traced_graph.recompile()
    node_users_map = {node.name: set(node.users.keys()) for node in traced_graph.graph.nodes }

    fused_graph = get_fused_graph(traced_graph)
    return rematerialize_fused_graph(fused_graph, node_users_map)

def rematerialize_stat(traced_graph, stat):
    global num_group_remat, memory_reduced, num_node_pairs
    # global no_weight_nodes
    # no_weight_nodes = {}

    num_group_remat = 0 
    memory_reduced = 0
    traced_graph.graph.eliminate_dead_code()
    traced_graph.recompile()
    node_users_map = {node.name: set(node.users.keys()) for node in traced_graph.graph.nodes }

    fused_graph = get_fused_graph(traced_graph)
    fused_graph = rematerialize_fused_graph(fused_graph, node_users_map)
    
    stat["num_group_remat"] = num_group_remat
    stat["memory_reduced"] = memory_reduced
    stat["num_node_pairs"] = num_node_pairs
    # print(no_weight_nodes)
    return fused_graph