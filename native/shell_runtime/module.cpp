#include "runtime.h"
#define DYNABRIDGE_IMPORT_DEF "import.def"
#define DYNABRIDGE_EXPORT_DEF "export.def"
#include "dynabridge/bridge.h"
#include "dynabridge/backends/python.h"

namespace dynabridge {
template<> struct py_backend::converter<pal_shell::id_t> {
    static PyObject* to(context_t&, pal_shell::id_t value) {
        return PyLong_FromUnsignedLongLong(value);
    }
    static optional<pal_shell::id_t> from(context_t&, PyObject* value) {
        if (!PyLong_CheckExact(value)) return {};
        auto converted = PyLong_AsUnsignedLongLong(value);
        if (PyErr_Occurred()) { PyErr_Clear(); return {}; }
        return optional<pal_shell::id_t>(converted);
    }
};
template<> struct py_backend::converter<bool> {
    static PyObject* to(context_t&, bool value) { return PyBool_FromLong(value); }
    static optional<bool> from(context_t&, PyObject* value) {
        if (!PyBool_Check(value)) return {};
        return optional<bool>(value == Py_True);
    }
};
template<> struct py_backend::converter<pal_shell::Event> {
    static PyObject* to(context_t&, const pal_shell::Event& event) {
        py_backend::object_ref result(PyDict_New(), py_backend::ref_policy::owned);
        if (!result) throw std::runtime_error("event allocation failed");
        auto put = [&result](const char* key, PyObject* raw) {
            py_backend::object_ref value(raw, py_backend::ref_policy::owned);
            if (!value || PyDict_SetItemString(result.get(), key, value.get()))
                throw std::runtime_error("event conversion failed");
        };
        put("request_id", PyLong_FromUnsignedLongLong(event.request));
        put("session_id", PyLong_FromUnsignedLongLong(event.session));
        put("output_id", PyLong_FromUnsignedLongLong(event.output));
        put("status", PyUnicode_FromString(event.status.c_str()));
        put("error", PyUnicode_DecodeUTF8(event.error.data(), event.error.size(), "replace"));
        put("stdout_path", PyUnicode_DecodeFSDefault(event.stdout_path.c_str()));
        put("stderr_path", PyUnicode_DecodeFSDefault(event.stderr_path.c_str()));
        if (event.inline_output) {
            put("stdout_bytes", PyBytes_FromStringAndSize(event.stdout_bytes.data(), event.stdout_bytes.size()));
            put("stderr_bytes", PyBytes_FromStringAndSize(event.stderr_bytes.data(), event.stderr_bytes.size()));
        }
        put("stdout_total", PyLong_FromUnsignedLongLong(event.stdout_total));
        put("stderr_total", PyLong_FromUnsignedLongLong(event.stderr_total));
        put("truncated", PyBool_FromLong(event.truncated));
        put("tty", PyBool_FromLong(event.tty));
        put("returncode", event.has_returncode ? PyLong_FromLong(event.returncode) : Py_NewRef(Py_None));
        put("signal", PyLong_FromLong(event.signal));
        return result.release();
    }
};
}

static PyModuleDef module_definition = {
    PyModuleDef_HEAD_INIT, "_pal_shell_runtime", "Isolated native shell prototype.", -1,
    nullptr, nullptr, nullptr, nullptr, nullptr
};

PyMODINIT_FUNC PyInit__pal_shell_runtime() {
    PyObject* result = PyModule_Create(&module_definition);
    if (!result) return nullptr;
    try {
        // Generated callables borrow their export context. Keep registration
        // metadata for the process lifetime, like a static extension type; do
        // not DECREF its types from a C++ destructor after Py_Finalize.
        // Runtime instances and their worker resources remain explicitly owned.
        static auto* registration = new dynabridge::py_backend::export_context_t;
        auto& ctx = *registration;
        dynabridge::py_backend::module_t module(result, dynabridge::py_backend::ref_policy::borrowed);
        dynabridge::exports::Runtime::register_all(ctx, module);
        using Signature = void(
            dynabridge::object_param<dynabridge::export_classes::Runtime, dynabridge::export_t>,
            dynabridge::callable_param<dynabridge::import_symbols::notify, dynabridge::import_t>);
        dynabridge::export_connect<Signature>(ctx, module, [](dynabridge::pal_shell::Runtime& runtime, auto& callback) {
            using Context = std::decay_t<decltype(callback)>;
            auto context = std::make_shared<Context>(std::move(callback));
            runtime.bind([context](const dynabridge::pal_shell::Event& event) {
                dynabridge::call_notify(*context, event);
            });
        });
        return result;
    } catch (const std::exception& error) {
        Py_DECREF(result);
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
}
