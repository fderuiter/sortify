"""Context-propagating concurrency primitives.

``contextvars`` are not inherited by plain ``threading.Thread`` or
``ThreadPoolExecutor`` workers. The helpers here copy the submitting context so
per-task state (for example the network sandbox flag) follows the work.
"""

import concurrent.futures
import contextvars
import threading


class ContextVarLocal:
    """Thread-local storage replacement powered by contextvars.ContextVar.

    This class provides a dictionary-like interface where attributes are stored
    in a contextvar, making them isolated to asynchronous/concurrent tasks.
    """

    def __init__(self):
        """Initialize the local context var dictionary."""
        super().__setattr__(
            "_var", contextvars.ContextVar("context_var_local", default={})
        )

    def __getattr__(self, name):
        """Retrieve an attribute value from the thread-local context dictionary.

        Parameters
        ----------
        name : str
            The attribute name to retrieve.

        Returns
        -------
        any
            The value of the retrieved attribute.

        Raises
        ------
        AttributeError
            If the attribute does not exist or _var is uninitialized.
        """
        if name == "_var":
            raise AttributeError("_var is not initialized")
        d = self._var.get()
        if name in d:
            return d[name]
        raise AttributeError(f"'ContextVarLocal' object has no attribute '{name}'")

    def __setattr__(self, name, value):
        """Set an attribute value in the thread-local context dictionary.

        Parameters
        ----------
        name : str
            The attribute name to set.
        value : any
            The value to assign.
        """
        if name == "_var":
            super().__setattr__(name, value)
            return
        d = dict(self._var.get())
        d[name] = value
        self._var.set(d)

    def __delattr__(self, name):
        """Delete an attribute from the thread-local context dictionary.

        Parameters
        ----------
        name : str
            The attribute name to delete.

        Raises
        ------
        AttributeError
            If the attribute does not exist or _var is uninitialized.
        """
        if name == "_var":
            super().__delattr__(name)
            return
        d = dict(self._var.get())
        if name in d:
            del d[name]
            self._var.set(d)
        else:
            raise AttributeError(f"'ContextVarLocal' object has no attribute '{name}'")


class ContextPropagatingThread(threading.Thread):
    """A thread subclass that copies and propagates the current contextvars Context.

    This ensures thread-local/contextvar attributes (e.g., sandboxed state) are
    correctly carried over into child threads.
    """

    def __init__(self, *args, **kwargs):
        """Initialize ContextPropagatingThread with copied context."""
        self._ctx = contextvars.copy_context()
        super().__init__(*args, **kwargs)

    def run(self):
        """Execute the thread target within the propagated context."""

        def wrapped():
            if (
                "VectorReconstruction" in self.name
                or "reconstruction" in self.name.lower()
            ):
                from app.core import network_guard

                network_guard.install()
                network_guard._thread_local.sandboxed = True
                network_guard._thread_local.reason = "background vector reconstruction"
            threading.Thread.run(self)

        self._ctx.run(wrapped)


class ContextPropagatingThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """A ThreadPoolExecutor that ensures contextvars context is propagated.

    This guarantees tasks submitted to the pool execute under the exact context
    active at submission time.
    """

    def submit(self, fn, *args, **kwargs):
        """Submit a callable to the pool, wrapping it to run inside the active context.

        Parameters
        ----------
        fn : callable
            The function to execute.
        *args : tuple
            Positional arguments for the callable.
        **kwargs : dict
            Keyword arguments for the callable.

        Returns
        -------
        concurrent.futures.Future
            A future representing the execution state of the callable.
        """
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)
