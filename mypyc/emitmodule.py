"""Generate C code for a Python C extension module from Python source code."""

# FIXME: Basically nothing in this file operates on the level of a
# single module and it should be renamed.

from collections import OrderedDict
from typing import List, Tuple, Dict, Iterable, Set, TypeVar, Optional

from mypy.build import BuildSource, BuildResult, build
from mypy.errors import CompileError
from mypy.options import Options

from mypyc import genops
from mypyc.common import PREFIX, TOP_LEVEL_NAME, INT_PREFIX, MODULE_PREFIX, shared_lib_name
from mypyc.emit import EmitterContext, Emitter, HeaderDeclaration
from mypyc.emitfunc import generate_native_function, native_function_header
from mypyc.emitclass import generate_class_type_decl, generate_class
from mypyc.emitwrapper import (
    generate_wrapper_function, wrapper_function_header,
)
from mypyc.ops import (
    FuncIR, ClassIR, ModuleIR, ModuleIRs, LiteralsMap, RType, RTuple
)
from mypyc.options import CompilerOptions
from mypyc.uninit import insert_uninit_checks
from mypyc.refcount import insert_ref_count_opcodes
from mypyc.exceptions import insert_exception_handling
from mypyc.namegen import NameGenerator, exported_name
from mypyc.errors import Errors


# All of the modules being compiled are divided into "groups". A group
# is a set of modules that are placed into the same shared library.
# Two common configurations are that every module is placed in a group
# by itself (fully separate compilation) and that every module is
# placed in the same group (fully whole-program compilation), but we
# support finer-grained control of the group as well.
#
# In fully whole-program compilation, we will generate N+1 extension
# modules: one shim per module and one shared library containing all
# the actual code.
# In fully separate compilation, we (unfortunately) will generate 2*N
# extension modules: one shim per module and also one library containg
# each module's actual code. (This might be fixable in the future,
# but allows a clean separation between setup of the export tables
# (see generate_export_table) and running module top levels.)
#
# A group is represented as a list of BuildSources containing all of
# its modules along with the name of the group. (Which can be None
# only if we are compiling only a single group with a single file in it
# and not using shared libraries).
Group = Tuple[List[BuildSource], Optional[str]]
Groups = List[Group]

# A list of (file name, file contents) pairs.
FileContents = List[Tuple[str, str]]


class MarkedDeclaration:
    """Add a mark, useful for topological sort."""
    def __init__(self, declaration: HeaderDeclaration, mark: bool) -> None:
        self.declaration = declaration
        self.mark = False


def parse_and_typecheck(sources: List[BuildSource], options: Options,
                        alt_lib_path: Optional[str] = None) -> BuildResult:
    assert options.strict_optional, 'strict_optional must be turned on'
    result = build(sources=sources,
                   options=options,
                   alt_lib_path=alt_lib_path)
    if result.errors:
        raise CompileError(result.errors)
    return result


def compile_modules_to_c(
    result: BuildResult,
    compiler_options: CompilerOptions,
    errors: Errors,
    groups: Groups,
) -> Tuple[ModuleIRs, List[FileContents]]:
    """Compile Python module(s) to the source of Python C extension modules.

    This generates the source code for the "shared library" module
    for each group. The shim modules are generated in mypyc.build.
    Each shared library module provides, for each module in its group,
    a PyCapsule containing an initialization function.
    Additionally, it provides a capsule containing an export table of
    pointers to all of the group's functions and static variables.

    Arguments:
        result: The BuildResult from the mypy front-end
        compiler_options: The compilation options
        errors: Where to report any errors encountered
        groups: The groups that we are compiling. See documentation of Groups type above.
        ops: Optionally, where to dump stringified ops for debugging.

    Returns the IR of the modules and a list containing the generated files for each group.
    """
    module_names = [source.module for group_sources, _ in groups for source in group_sources]
    file_nodes = [result.files[name] for name in module_names]

    # Construct a map from modules to what group they belong to
    group_map = {}
    for group, lib_name in groups:
        for source in group:
            group_map[source.module] = lib_name

    # Generate basic IR, with missing exception and refcount handling.
    mapper = genops.Mapper(group_map)
    modules = genops.build_ir(file_nodes, result.graph, result.types,
                              mapper,
                              compiler_options, errors)
    if errors.num_errors > 0:
        return modules, []
    # Insert uninit checks.
    for module in modules.values():
        for fn in module.functions:
            insert_uninit_checks(fn)
    # Insert exception handling.
    for module in modules.values():
        for fn in module.functions:
            insert_exception_handling(fn)
    # Insert refcount handling.
    for module in modules.values():
        for fn in module.functions:
            insert_ref_count_opcodes(fn)

    source_paths = {module_name: result.files[module_name].path
                    for module_name in module_names}

    names = NameGenerator([[source.module for source in sources] for sources, _ in groups])

    # Generate C code for each compilation group. Each group will be
    # compiled into a separate extension module.
    ctext = []
    for group_sources, group_name in groups:
        group_modules = [(source.module, modules[source.module]) for source in group_sources]
        literals = mapper.literals[group_name]
        generator = GroupGenerator(
            literals, group_modules, source_paths, group_name, group_map, names,
            compiler_options.multi_file
        )
        ctext.append(generator.generate_c_for_modules())
    return modules, ctext


def generate_function_declaration(fn: FuncIR, emitter: Emitter) -> None:
    emitter.context.declarations[emitter.native_function_name(fn.decl)] = HeaderDeclaration(
        '{};'.format(native_function_header(fn.decl, emitter)),
        needs_export=True)
    if fn.name != TOP_LEVEL_NAME:
        emitter.context.declarations[PREFIX + fn.cname(emitter.names)] = HeaderDeclaration(
            '{};'.format(wrapper_function_header(fn, emitter.names)))


def encode_as_c_string(s: str) -> Tuple[str, int]:
    """Produce a utf-8 encoded, escaped, quoted C string and its size from a string"""
    # This is a kind of abusive way to do this...
    b = s.encode('utf-8')
    escaped = str(b)[2:-1].replace('"', '\\"')
    return '"{}"'.format(escaped), len(b)


def encode_bytes_as_c_string(b: bytes) -> Tuple[str, int]:
    """Produce a single-escaped, quoted C string and its size from a bytes"""
    # This is a kind of abusive way to do this...
    escaped = str(b)[2:-1].replace('"', '\\"')
    return '"{}"'.format(escaped), len(b)


def pointerize(decl: str, name: str) -> str:
    """Given a C decl and its name, modify it to be a declaration to a pointer."""
    # This doesn't work in general but does work for all our types...
    if '(' in decl:
        # Function pointer. Stick a * in front of the name and wrap it in parens.
        return decl.replace(name, '(*{})'.format(name))
    else:
        # Non-function pointer. Just stick a * in front of the name.
        return decl.replace(name, '*{}'.format(name))


class GroupGenerator:
    def __init__(self,
                 literals: LiteralsMap,
                 modules: List[Tuple[str, ModuleIR]],
                 source_paths: Dict[str, str],
                 group_name: Optional[str],
                 group_map: Dict[str, Optional[str]],
                 names: NameGenerator,
                 multi_file: bool) -> None:
        """Generator for C source for a compilation group.

        The code for a compilation group contains an internal and an
        external .h file, and then one .c if not in multi_file mode or
        one .c file per module if in multi_file mode.)

        Arguments:
            literals: The literals declared in this group
            modules: (name, ir) pairs for each module in the group
            source_paths: Map from module names to source file paths
            group_name: The name of the group (or None if this is single-module compilation)
            group_map: A map of modules to their group names
            names: The name generator for the compilation
            multi_file: Whether to put each module in its own source file regardless
                        of group structure.
        """
        self.literals = literals
        self.modules = modules
        self.source_paths = source_paths
        self.context = EmitterContext(names, group_name, group_map)
        self.names = names
        # Initializations of globals to simple values that we can't
        # do statically because the windows loader is bad.
        self.simple_inits = []  # type: List[Tuple[str, str]]
        self.group_name = group_name
        self.use_shared_lib = group_name is not None
        self.multi_file = multi_file

    @property
    def group_suffix(self) -> str:
        return '_' + self.group_name if self.group_name else ''

    def generate_c_for_modules(self) -> List[Tuple[str, str]]:
        file_contents = []
        multi_file = self.use_shared_lib and self.multi_file

        base_emitter = Emitter(self.context)
        base_emitter.emit_line('#include "__native{}.h"'.format(self.group_suffix))
        base_emitter.emit_line('#include "__native_internal{}.h"'.format(self.group_suffix))
        emitter = base_emitter

        for (_, literal), identifier in self.literals.items():
            if isinstance(literal, int):
                symbol = emitter.static_name(identifier, None)
                self.declare_global('CPyTagged ', symbol)
            else:
                self.declare_static_pyobject(identifier, emitter)

        for module_name, module in self.modules:
            if multi_file:
                emitter = Emitter(self.context)
                emitter.emit_line('#include "__native{}.h"'.format(self.group_suffix))
                emitter.emit_line('#include "__native_internal{}.h"'.format(self.group_suffix))

            self.declare_module(module_name, emitter)
            self.declare_internal_globals(module_name, emitter)
            self.declare_imports(module.imports, emitter)

            for cl in module.classes:
                if cl.is_ext_class:
                    generate_class(cl, module_name, emitter)

            # Generate Python extension module definitions and module initialization functions.
            self.generate_module_def(emitter, module_name, module)

            for fn in module.functions:
                emitter.emit_line()
                generate_native_function(fn, emitter, self.source_paths[module_name], module_name)
                if fn.name != TOP_LEVEL_NAME:
                    emitter.emit_line()
                    generate_wrapper_function(
                        fn, emitter, self.source_paths[module_name], module_name)

            if multi_file:
                name = ('__native_{}.c'.format(emitter.names.private_name(module_name)))
                file_contents.append((name, ''.join(emitter.fragments)))

        # The external header file contains type declarations while
        # the internal contains declarations of functions and objects
        # (which are shared between shared libraries via dynamic
        # exports tables and not accessed directly.)
        ext_declarations = Emitter(self.context)
        ext_declarations.emit_line('#ifndef MYPYC_NATIVE{}_H'.format(self.group_suffix))
        ext_declarations.emit_line('#define MYPYC_NATIVE{}_H'.format(self.group_suffix))
        ext_declarations.emit_line('#include <Python.h>')
        ext_declarations.emit_line('#include <CPy.h>')

        declarations = Emitter(self.context)
        declarations.emit_line('#ifndef MYPYC_NATIVE_INTERNAL{}_H'.format(self.group_suffix))
        declarations.emit_line('#define MYPYC_NATIVE_INTERNAL{}_H'.format(self.group_suffix))
        declarations.emit_line('#include <Python.h>')
        declarations.emit_line('#include <CPy.h>')
        declarations.emit_line('#include "__native{}.h"'.format(self.group_suffix))
        declarations.emit_line()
        declarations.emit_line('int CPyGlobalsInit(void);')
        declarations.emit_line()

        for module_name, module in self.modules:
            self.declare_finals(module_name, module.final_names, declarations)
            for cl in module.classes:
                generate_class_type_decl(cl, emitter, ext_declarations, declarations)
            for fn in module.functions:
                generate_function_declaration(fn, declarations)

        for lib in sorted(self.context.group_deps):
            declarations.emit_lines(
                '#include "__native_{}.h"'.format(lib),
                'struct export_table_{} exports_{};'.format(lib, lib)
            )

        sorted_decls = self.toposort_declarations()

        emitter = base_emitter
        self.generate_globals_init(emitter)

        emitter.emit_line()

        for declaration in sorted_decls:
            decls = ext_declarations if declaration.is_type else declarations
            if not declaration.is_type:
                decls.emit_lines(
                    'extern {}'.format(declaration.decl[0]), *declaration.decl[1:])
                # If there is a definition, emit it. Otherwise repeat the declaration
                # (without an extern).
                if declaration.defn:
                    emitter.emit_lines(*declaration.defn)
                else:
                    emitter.emit_lines(*declaration.decl)
            else:
                decls.emit_lines(*declaration.decl)

        if self.group_name:
            self.generate_export_table(ext_declarations, emitter)

            self.generate_shared_lib_init(emitter)

        ext_declarations.emit_line('#endif')
        declarations.emit_line('#endif')

        return file_contents + [('__native{}.c'.format(self.group_suffix),
                                 ''.join(emitter.fragments)),
                                ('__native_internal{}.h'.format(self.group_suffix),
                                 ''.join(declarations.fragments)),
                                ('__native{}.h'.format(self.group_suffix),
                                 ''.join(ext_declarations.fragments)),
                                ]

    def generate_export_table(self, decl_emitter: Emitter, code_emitter: Emitter) -> None:
        """Generate the declaration and definition of the group's export struct.

        To avoid needing to deal with deeply platform specific issues
        involving dynamic library linking (and some possibly
        insurmountable issues involving cyclic dependencies), compiled
        code accesses functions and data in other compilation groups
        via an explicit "export struct".

        Each group declares a struct type that contains a pointer to
        every function and static variable it exports. It then
        populates this struct and stores a pointer to it in a capsule
        stored as an attribute named 'exports' on the group's shared
        library's python module.

        On load, a group's init function will import all of its
        dependencies' exports tables using the capsule mechanism and
        copy the contents into a local copy of the table (to eliminate
        the need for a pointer indirection when accessing it).

        Then, all calls to functions in another group and accesses to statics
        from another group are done indirectly via the export table.

        For example, a group containing a module b, where b contains a class B
        and a function bar, would declare an export table like:
            struct export_table_b {
                PyTypeObject **CPyType_B;
                PyObject *(*CPyDef_B)(CPyTagged cpy_r_x);
                CPyTagged (*CPyDef_B___foo)(PyObject *cpy_r_self, CPyTagged cpy_r_y);
                tuple_T2OI (*CPyDef_bar)(PyObject *cpy_r_x);
                char (*CPyDef___top_level__)(void);
            };
        that would be initialized with:
            static struct export_table_b exports = {
                &CPyType_B,
                &CPyDef_B,
                &CPyDef_B___foo,
                &CPyDef_bar,
                &CPyDef___top_level__,
            };
        To call `b.foo`, then, a function in another group would do
        `exports_b.CPyDef_bar(...)`.
        """

        decls = decl_emitter.context.declarations

        decl_emitter.emit_lines(
            '',
            'struct export_table{} {{'.format(self.group_suffix),
        )
        for name, decl in decls.items():
            if decl.needs_export:
                decl_emitter.emit_line(pointerize('\n'.join(decl.decl), name))

        decl_emitter.emit_line('};')

        code_emitter.emit_lines(
            '',
            'static struct export_table{} exports = {{'.format(self.group_suffix),
        )
        for name, decl in decls.items():
            if decl.needs_export:
                code_emitter.emit_line('&{},'.format(name))

        code_emitter.emit_line('};')

    def generate_shared_lib_init(self, emitter: Emitter) -> None:
        """Generate the init function for a shared library.

        A shared library contains all of the actual code for a
        compilation group.

        The init function is responsible for creating Capsules that
        wrap pointers to the initialization function of all the real
        init functions for modules in this shared library as well as
        the export table containing all of the exported functions and
        values from all the modules.

        These capsules are stored in attributes of the shared library.
        """
        assert self.group_name is not None

        emitter.emit_line()
        emitter.emit_lines(
            'PyMODINIT_FUNC PyInit_{}(void)'.format(shared_lib_name(self.group_name)),
            '{',
            ('static PyModuleDef def = {{ PyModuleDef_HEAD_INIT, "{}", NULL, -1, NULL, NULL }};'
             .format(self.group_name)),
            'int res;',
            'PyObject *capsule;',
            'static PyObject *module;',
            'if (module) {',
            'Py_INCREF(module);',
            'return module;',
            '}',
            'module = PyModule_Create(&def);',
            'if (!module) {',
            'goto fail;',
            '}',
            '',
        )

        emitter.emit_lines(
            'capsule = PyCapsule_New(&exports, "{}.exports", NULL);'.format(
                shared_lib_name(self.group_name)),
            'if (!capsule) {',
            'goto fail;',
            '}',
            'res = PyObject_SetAttrString(module, "exports", capsule);',
            'Py_DECREF(capsule);',
            'if (res < 0) {',
            'goto fail;',
            '}',
            '',
        )

        for mod, _ in self.modules:
            name = exported_name(mod)
            emitter.emit_lines(
                'extern PyObject *CPyInit_{}(void);'.format(name),
                'capsule = PyCapsule_New((void *)CPyInit_{}, "{}.init_{}", NULL);'.format(
                    name, shared_lib_name(self.group_name), name),
                'if (!capsule) {',
                'goto fail;',
                '}',
                'res = PyObject_SetAttrString(module, "init_{}", capsule);'.format(name),
                'Py_DECREF(capsule);',
                'if (res < 0) {',
                'goto fail;',
                '}',
                '',
            )

        for group in sorted(self.context.group_deps):
            emitter.emit_lines(
                'struct export_table_{} *pexports_{} = PyCapsule_Import("{}.exports", 0);'.format(
                    group, group, shared_lib_name(group)),
                'if (!pexports_{}) {{'.format(group),
                'goto fail;',
                '}',
                'memcpy(&exports_{group}, pexports_{group}, sizeof(exports_{group}));'.format(
                    group=group),
                '',
            )

        emitter.emit_lines(
            'return module;',
            'fail:',
            'Py_XDECREF(module);',
            'return NULL;',
            '}',
        )

    def generate_globals_init(self, emitter: Emitter) -> None:
        emitter.emit_lines(
            '',
            'int CPyGlobalsInit(void)',
            '{',
            'static int is_initialized = 0;',
            'if (is_initialized) return 0;',
            ''
        )

        emitter.emit_line('CPy_Init();')
        for symbol, fixup in self.simple_inits:
            emitter.emit_line('{} = {};'.format(symbol, fixup))

        for (_, literal), identifier in self.literals.items():
            symbol = emitter.static_name(identifier, None)
            if isinstance(literal, int):
                actual_symbol = symbol
                symbol = INT_PREFIX + symbol
                emitter.emit_line(
                    'PyObject * {} = PyLong_FromString(\"{}\", NULL, 10);'.format(
                        symbol, str(literal))
                )
            elif isinstance(literal, float):
                emitter.emit_line(
                    '{} = PyFloat_FromDouble({});'.format(symbol, str(literal))
                )
            elif isinstance(literal, complex):
                emitter.emit_line(
                    '{} = PyComplex_FromDoubles({}, {});'.format(
                        symbol, str(literal.real), str(literal.imag))
                )
            elif isinstance(literal, str):
                emitter.emit_line(
                    '{} = PyUnicode_FromStringAndSize({}, {});'.format(
                        symbol, *encode_as_c_string(literal))
                )
            elif isinstance(literal, bytes):
                emitter.emit_line(
                    '{} = PyBytes_FromStringAndSize({}, {});'.format(
                        symbol, *encode_bytes_as_c_string(literal))
                )
            else:
                assert False, ('Literals must be integers, floating point numbers, or strings,',
                               'but the provided literal is of type {}'.format(type(literal)))
            emitter.emit_lines('if (unlikely({} == NULL))'.format(symbol),
                               '    return -1;')
            # Ints have an unboxed representation.
            if isinstance(literal, int):
                emitter.emit_line(
                    '{} = CPyTagged_FromObject({});'.format(actual_symbol, symbol)
                )

        emitter.emit_lines(
            'is_initialized = 1;',
            'return 0;',
            '}',
        )

    def generate_module_def(self, emitter: Emitter, module_name: str, module: ModuleIR) -> None:
        """Emit the PyModuleDef struct for a module and the module init function."""
        # Emit module methods
        module_prefix = emitter.names.private_name(module_name)
        emitter.emit_line('static PyMethodDef {}module_methods[] = {{'.format(module_prefix))
        for fn in module.functions:
            if fn.class_name is not None or fn.name == TOP_LEVEL_NAME:
                continue
            emitter.emit_line(
                ('{{"{name}", (PyCFunction){prefix}{cname}, METH_VARARGS | METH_KEYWORDS, '
                 'NULL /* docstring */}},').format(
                    name=fn.name,
                    cname=fn.cname(emitter.names),
                    prefix=PREFIX))
        emitter.emit_line('{NULL, NULL, 0, NULL}')
        emitter.emit_line('};')
        emitter.emit_line()

        # Emit module definition struct
        emitter.emit_lines('static struct PyModuleDef {}module = {{'.format(module_prefix),
                           'PyModuleDef_HEAD_INIT,',
                           '"{}",'.format(module_name),
                           'NULL, /* docstring */',
                           '-1,       /* size of per-interpreter state of the module,',
                           '             or -1 if the module keeps state in global variables. */',
                           '{}module_methods'.format(module_prefix),
                           '};')
        emitter.emit_line()
        # Emit module init function. If we are compiling just one module, this
        # will be the C API init function. If we are compiling 2+ modules, we
        # generate a shared library for the modules and shims that call into
        # the shared library, and in this case we use an internal module
        # initialized function that will be called by the shim.
        if not self.use_shared_lib:
            declaration = 'PyMODINIT_FUNC PyInit_{}(void)'.format(module_name)
        else:
            declaration = 'PyObject *CPyInit_{}(void)'.format(exported_name(module_name))
        emitter.emit_lines(declaration,
                           '{')
        # Store the module reference in a static and return it when necessary.
        # This is separate from the *global* reference to the module that will
        # be populated when it is imported by a compiled module. We want that
        # reference to only be populated when the module has been succesfully
        # imported, whereas this we want to have to stop a circular import.
        module_static = self.module_internal_static_name(module_name, emitter)

        emitter.emit_lines('if ({}) {{'.format(module_static),
                           'Py_INCREF({});'.format(module_static),
                           'return {};'.format(module_static),
                           '}')

        emitter.emit_lines('{} = PyModule_Create(&{}module);'.format(module_static, module_prefix),
                           'if (unlikely({} == NULL))'.format(module_static),
                           '    return NULL;')
        emitter.emit_line(
            'PyObject *modname = PyObject_GetAttrString((PyObject *){}, "__name__");'.format(
                module_static))

        module_globals = emitter.static_name('globals', module_name)
        emitter.emit_lines('{} = PyModule_GetDict({});'.format(module_globals, module_static),
                           'if (unlikely({} == NULL))'.format(module_globals),
                           '    return NULL;')

        # HACK: Manually instantiate generated classes here
        for cl in module.classes:
            if cl.is_generated:
                type_struct = emitter.type_struct_name(cl)
                emitter.emit_lines(
                    '{t} = (PyTypeObject *)CPyType_FromTemplate({t}_template, NULL, modname);'.
                    format(t=type_struct))
                emitter.emit_lines('if (unlikely(!{}))'.format(type_struct),
                                   '    return NULL;')

        emitter.emit_lines('if (CPyGlobalsInit() < 0)',
                           '    return NULL;')

        self.generate_top_level_call(module, emitter)

        emitter.emit_lines('Py_DECREF(modname);')

        emitter.emit_line('return {};'.format(module_static))
        emitter.emit_line('}')

    def generate_top_level_call(self, module: ModuleIR, emitter: Emitter) -> None:
        """Generate call to function representing module top level."""
        # Optimization: we tend to put the top level last, so reverse iterate
        for fn in reversed(module.functions):
            if fn.name == TOP_LEVEL_NAME:
                emitter.emit_lines(
                    'char result = {}();'.format(emitter.native_function_name(fn.decl)),
                    'if (result == 2)',
                    '    return NULL;',
                )
                break

    def toposort_declarations(self) -> List[HeaderDeclaration]:
        """Topologically sort the declaration dict by dependencies.

        Declarations can require other declarations to come prior in C (such as declaring structs).
        In order to guarantee that the C output will compile the declarations will thus need to
        be properly ordered. This simple DFS guarantees that we have a proper ordering.

        This runs in O(V + E).
        """
        result = []
        marked_declarations = OrderedDict()  # type: Dict[str, MarkedDeclaration]
        for k, v in self.context.declarations.items():
            marked_declarations[k] = MarkedDeclaration(v, False)

        def _toposort_visit(name: str) -> None:
            decl = marked_declarations[name]
            if decl.mark:
                return

            for child in decl.declaration.dependencies:
                _toposort_visit(child)

            result.append(decl.declaration)
            decl.mark = True

        for name, marked_declaration in marked_declarations.items():
            _toposort_visit(name)

        return result

    def declare_global(self, type_spaced: str, name: str,
                       *,
                       initializer: Optional[str] = None) -> None:
        if not initializer:
            defn = None
        else:
            defn = ['{}{} = {};'.format(type_spaced, name, initializer)]
        if name not in self.context.declarations:
            self.context.declarations[name] = HeaderDeclaration(
                '{}{};'.format(type_spaced, name),
                defn=defn,
            )

    def declare_internal_globals(self, module_name: str, emitter: Emitter) -> None:
        static_name = emitter.static_name('globals', module_name)
        self.declare_global('PyObject *', static_name)

    def module_internal_static_name(self, module_name: str, emitter: Emitter) -> str:
        return emitter.static_name(module_name + '_internal', None, prefix=MODULE_PREFIX)

    def declare_module(self, module_name: str, emitter: Emitter) -> None:
        # We declare two globals for each module:
        # one used internally in the implementation of module init to cache results
        # and prevent infinite recursion in import cycles, and one used
        # by other modules to refer to it.
        internal_static_name = self.module_internal_static_name(module_name, emitter)
        self.declare_global('CPyModule *', internal_static_name, initializer='NULL')
        static_name = emitter.static_name(module_name, None, prefix=MODULE_PREFIX)
        self.declare_global('CPyModule *', static_name)
        self.simple_inits.append((static_name, 'Py_None'))

    def declare_imports(self, imps: Iterable[str], emitter: Emitter) -> None:
        for imp in imps:
            self.declare_module(imp, emitter)

    def declare_finals(
            self, module: str, final_names: Iterable[Tuple[str, RType]], emitter: Emitter) -> None:
        for name, typ in final_names:
            static_name = emitter.static_name(name, module)
            emitter.context.declarations[static_name] = HeaderDeclaration(
                '{}{};'.format(emitter.ctype_spaced(typ), static_name),
                [self.final_definition(module, name, typ, emitter)],
                needs_export=True)

    def final_definition(
            self, module: str, name: str, typ: RType, emitter: Emitter) -> str:
        static_name = emitter.static_name(name, module)
        # Here we rely on the fact that undefined value and error value are always the same
        if isinstance(typ, RTuple):
            # We need to inline because initializer must be static
            undefined = '{{ {} }}'.format(''.join(emitter.tuple_undefined_value_helper(typ)))
        else:
            undefined = emitter.c_undefined_value(typ)
        return '{}{} = {};'.format(emitter.ctype_spaced(typ), static_name, undefined)

    def declare_static_pyobject(self, identifier: str, emitter: Emitter) -> None:
        symbol = emitter.static_name(identifier, None)
        self.declare_global('PyObject *', symbol)


def sort_classes(classes: List[Tuple[str, ClassIR]]) -> List[Tuple[str, ClassIR]]:
    mod_name = {ir: name for name, ir in classes}
    irs = [ir for _, ir in classes]
    deps = OrderedDict()  # type: Dict[ClassIR, Set[ClassIR]]
    for ir in irs:
        if ir not in deps:
            deps[ir] = set()
        if ir.base:
            deps[ir].add(ir.base)
        deps[ir].update(ir.traits)
    sorted_irs = toposort(deps)
    return [(mod_name[ir], ir) for ir in sorted_irs]


T = TypeVar('T')


def toposort(deps: Dict[T, Set[T]]) -> List[T]:
    """Topologically sort a dict from item to dependencies.

    This runs in O(V + E).
    """
    result = []
    visited = set()  # type: Set[T]

    def visit(item: T) -> None:
        if item in visited:
            return

        for child in deps[item]:
            visit(child)

        result.append(item)
        visited.add(item)

    for item in deps:
        visit(item)

    return result
