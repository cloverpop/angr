
import logging
import pickle
import time
import os
import tempfile
import multiprocessing

from .worker import Worker


_l = logging.getLogger(__name__)
_l.setLevel(logging.DEBUG)


class Server:
    """
    Server implements the analysis server with a series of control interfaces exposed.

    :ivar project:          An instance of angr.Project.
    :ivar str spill_yard:   A directory to store spilled states.
    :ivar str db:           Path of the database that stores information about spilled states.
    :ivar int max_workers:  Maximum number of workers. Each worker starts a new process.
    :ivar int max_states:   Maximum number of active states for each worker.
    :ivar int staging_max:  Maximum number of inactive states that are kept into memory before spilled onto the disk
                            and potentially be picked up by another worker.
    :ivar bool bucketizer:  Use the Bucketizer exploration strategy.
    :ivar _worker_exit_callback:    A method that will be called upon the exit of each worker.
    """
    def __init__(self, project, spill_yard=None, db=None, max_workers=None, max_states=10, staging_max=10,
                 bucketizer=True, worker_exit_callback=None):

        self.project = project

        self.spill_yard = spill_yard if spill_yard else tempfile.mkdtemp(suffix="angr_spill_yard")
        if not spill_yard:
            _l.info("Temporary spill yard: %s", self.spill_yard)
        self.db_str = db if db else "sqlite:///" + os.path.join(tempfile.mkdtemp(suffix="angr_server_db"), "db.sqlite3")
        if not db:
            _l.info("Database: %s", self.db_str)

        self.max_workers = max_workers if max_workers is not None else multiprocessing.cpu_count()
        self.max_states = max_states
        self.staging_max = staging_max
        self.bucketizer = bucketizer

        self._worker_exit_args_lock = None
        self._worker_exit_args = None  # type: dict[int:tuple]

        # the following will not be pickled
        self._worker_exit_callback = worker_exit_callback

        self._workers = [ ]
        self._stopped = False
        self._active_workers = multiprocessing.Value('i', lock=True)  # type: multiprocessing.Value

    def __setstate__(self, state):

        self.project = state['project']
        self.spill_yard = state['spill_yard']
        self.db_str = state['db_str']

        self.max_states = state['max_states']
        self.max_workers = state['max_workers']
        self.staging_max = state['staging_max']
        self.bucketizer = state['bucketizer']
        self._worker_exit_args_lock = state['_worker_exit_args_lock']
        self._worker_exit_args = state['_worker_exit_args']
        self._stopped = state['_stopped']
        self._active_workers = state['_active_workers']

    def __getstate__(self):
        s = {
            'project': self.project,
            'spill_yard': self.spill_yard,
            'db_str': self.db_str,
            'max_states': self.max_states,
            'max_workers': self.max_workers,
            'staging_max': self.staging_max,
            'bucketizer': self.bucketizer,
            '_worker_exit_args_lock': self._worker_exit_args_lock,
            '_worker_exit_args': self._worker_exit_args,
            '_stopped': self._stopped,
            '_active_workers': self._active_workers,
        }
        return s

    #
    # Actions
    #

    def inc_active_workers(self):
        with self._active_workers.get_lock():
            self._active_workers.value += 1

    def dec_active_workers(self):
        with self._active_workers.get_lock():
            self._active_workers.value -= 1

    def stop(self):
        self._stopped = True

    #
    # Properties
    #

    @property
    def active_workers(self):
        return self._active_workers.value

    @property
    def stopped(self):
        return self._stopped

    #
    # Callbacks
    #

    def on_worker_exit(self, worker_id, stashes):
        if self._worker_exit_args_lock is not None:
            # callback is enabled
            # we add this check since passing Python objects between processes is definitely not fast
            with self._worker_exit_args_lock:
                self._worker_exit_args[worker_id] = (worker_id, stashes, )

    #
    # Public methods
    #

    def run(self):

        # create workers
        with multiprocessing.Manager() as manager:
            if self._worker_exit_callback:
                # Do not initialize the lock if no callback is provided
                self._worker_exit_args_lock = manager.Lock()
                self._worker_exit_args = manager.dict()

            for i in range(self.max_workers):
                worker = Worker(i, self)
                self._workers.append(worker)

            # start them
            for w in self._workers:
                w.start()

            # should be enough for at least one child process to start
            time.sleep(3)

            i = 0
            while not self._stopped and self.active_workers > 0:
                time.sleep(1)

                if self._worker_exit_callback and self._worker_exit_args:
                    with self._worker_exit_args_lock:
                        for worker_id, args in self._worker_exit_args.items():
                            self._worker_exit_callback(*args)
