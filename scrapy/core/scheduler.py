from __future__ import annotations

import json
import logging
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# working around https://github.com/sphinx-doc/sphinx/issues/10400
from twisted.internet.defer import Deferred  # noqa: TC002

from scrapy.spiders import Spider  # noqa: TC001
from scrapy.utils.job import job_dir
from scrapy.utils.misc import build_from_crawler, load_object

if TYPE_CHECKING:
    # requires queuelib >= 1.6.2
    from queuelib.queue import BaseQueue

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy.crawler import Crawler
    from scrapy.dupefilters import BaseDupeFilter
    from scrapy.http.request import Request
    from scrapy.pqueues import ScrapyPriorityQueue
    from scrapy.statscollectors import StatsCollector


logger = logging.getLogger(__name__)


class BaseSchedulerMeta(type):
    """
    Metaclass to check scheduler classes against the necessary interface
    """

    def __instancecheck__(cls, instance: Any) -> bool:
        return cls.__subclasscheck__(type(instance))

    def __subclasscheck__(cls, subclass: type) -> bool:
        return (
            hasattr(subclass, "has_pending_requests")
            and callable(subclass.has_pending_requests)
            and hasattr(subclass, "enqueue_request")
            and callable(subclass.enqueue_request)
            and hasattr(subclass, "next_request")
            and callable(subclass.next_request)
        )


class BaseScheduler(metaclass=BaseSchedulerMeta):
    """
    The scheduler component is responsible for storing requests received from
    the engine, and feeding them back upon request (also to the engine).

    The original sources of said requests are:

    * Spider: ``start_requests`` method, requests created for URLs in the ``start_urls`` attribute, request callbacks
    * Spider middleware: ``process_spider_output`` and ``process_spider_exception`` methods
    * Downloader middleware: ``process_request``, ``process_response`` and ``process_exception`` methods

    The order in which the scheduler returns its stored requests (via the ``next_request`` method)
    plays a great part in determining the order in which those requests are downloaded.

    The methods defined in this class constitute the minimal interface that the Scrapy engine will interact with.
    """

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """
        Factory method which receives the current :class:`~scrapy.crawler.Crawler` object as argument.
        """
        return cls()

    def open(self, spider: Spider) -> Deferred[None] | None:
        """
        Called when the spider is opened by the engine. It receives the spider
        instance as argument and it's useful to execute initialization code.

        :param spider: the spider object for the current crawl
        :type spider: :class:`~scrapy.spiders.Spider`
        """

    def close(self, reason: str) -> Deferred[None] | None:
        """
        Called when the spider is closed by the engine. It receives the reason why the crawl
        finished as argument and it's useful to execute cleaning code.

        :param reason: a string which describes the reason why the spider was closed
        :type reason: :class:`str`
        """

    @abstractmethod
    def has_pending_requests(self) -> bool:
        """
        ``True`` if the scheduler has enqueued requests, ``False`` otherwise
        """
        raise NotImplementedError

    @abstractmethod
    def enqueue_request(self, request: Request) -> bool:
        """
        Process a request received by the engine.

        Return ``True`` if the request is stored correctly, ``False`` otherwise.

        If ``False``, the engine will fire a ``request_dropped`` signal, and
        will not make further attempts to schedule the request at a later time.
        For reference, the default Scrapy scheduler returns ``False`` when the
        request is rejected by the dupefilter.
        """
        raise NotImplementedError

    @abstractmethod
    def next_request(self) -> Request | None:
        """
        Return the next :class:`~scrapy.Request` to be processed, or ``None``
        to indicate that there are no requests to be considered ready at the moment.

        Returning ``None`` implies that no request from the scheduler will be sent
        to the downloader in the current reactor cycle. The engine will continue
        calling ``next_request`` until ``has_pending_requests`` is ``False``.
        """
        raise NotImplementedError


class Scheduler(BaseScheduler):
    """
    Default Scrapy scheduler. This implementation also handles duplication
    filtering via the :setting:`dupefilter <DUPEFILTER_CLASS>`.

    This scheduler stores requests into several priority queues (defined by the
    :setting:`SCHEDULER_PRIORITY_QUEUE` setting). In turn, said priority queues
    are backed by either memory or disk based queues (respectively defined by the
    :setting:`SCHEDULER_MEMORY_QUEUE` and :setting:`SCHEDULER_DISK_QUEUE` settings).

    Request prioritization is almost entirely delegated to the priority queue. The only
    prioritization performed by this scheduler is using the disk-based queue if present
    (i.e. if the :setting:`JOBDIR` setting is defined) and falling back to the memory-based
    queue if a serialization error occurs. If the disk queue is not present, the memory one
    is used directly.

    :param dupefilter: An object responsible for checking and filtering duplicate requests.
                       The value for the :setting:`DUPEFILTER_CLASS` setting is used by default.
    :type dupefilter: :class:`scrapy.dupefilters.BaseDupeFilter` instance or similar:
                      any class that implements the `BaseDupeFilter` interface

    :param jobdir: The path of a directory to be used for persisting the crawl's state.
                   The value for the :setting:`JOBDIR` setting is used by default.
                   See :ref:`topics-jobs`.
    :type jobdir: :class:`str` or ``None``

    :param dqclass: A class to be used as persistent request queue.
                    The value for the :setting:`SCHEDULER_DISK_QUEUE` setting is used by default.
    :type dqclass: class

    :param mqclass: A class to be used as non-persistent request queue.
                    The value for the :setting:`SCHEDULER_MEMORY_QUEUE` setting is used by default.
    :type mqclass: class

    :param logunser: A boolean that indicates whether or not unserializable requests should be logged.
                     The value for the :setting:`SCHEDULER_DEBUG` setting is used by default.
    :type logunser: bool

    :param stats: A stats collector object to record stats about the request scheduling process.
                  The value for the :setting:`STATS_CLASS` setting is used by default.
    :type stats: :class:`scrapy.statscollectors.StatsCollector` instance or similar:
                 any class that implements the `StatsCollector` interface

    :param pqclass: A class to be used as priority queue for requests.
                    The value for the :setting:`SCHEDULER_PRIORITY_QUEUE` setting is used by default.
    :type pqclass: class

    :param crawler: The crawler object corresponding to the current crawl.
    :type crawler: :class:`scrapy.crawler.Crawler`
    """

    def __init__(
        self,
        dupefilter: BaseDupeFilter,
        jobdir: str | None = None,
        dqclass: type[BaseQueue] | None = None,
        mqclass: type[BaseQueue] | None = None,
        logunser: bool = False,
        stats: StatsCollector | None = None,
        pqclass: type[ScrapyPriorityQueue] | None = None,
        crawler: Crawler | None = None,
    ):
        self.df: BaseDupeFilter = dupefilter
        self.dqdir: str | None = self._dqdir(jobdir)
        self.pqclass: type[ScrapyPriorityQueue] | None = pqclass
        self.dqclass: type[BaseQueue] | None = dqclass
        self.mqclass: type[BaseQueue] | None = mqclass
        self.logunser: bool = logunser
        self.stats: StatsCollector | None = stats
        self.crawler: Crawler | None = crawler

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """
        Factory method, initializes the scheduler with arguments taken from the crawl settings
        """
        dupefilter_cls = load_object(crawler.settings["DUPEFILTER_CLASS"])
        return cls(
            dupefilter=build_from_crawler(dupefilter_cls, crawler),
            jobdir=job_dir(crawler.settings),
            dqclass=load_object(crawler.settings["SCHEDULER_DISK_QUEUE"]),
            mqclass=load_object(crawler.settings["SCHEDULER_MEMORY_QUEUE"]),
            logunser=crawler.settings.getbool("SCHEDULER_DEBUG"),
            stats=crawler.stats,
            pqclass=load_object(crawler.settings["SCHEDULER_PRIORITY_QUEUE"]),
            crawler=crawler,
        )

    def has_pending_requests(self) -> bool:
        return len(self) > 0

    def open(self, spider: Spider) -> Deferred[None] | None:
        """
        (1) initialize the memory queue
        (2) initialize the disk queue if the ``jobdir`` attribute is a valid directory
        (3) return the result of the dupefilter's ``open`` method
        """
        self.spider: Spider = spider
        self.mqs: ScrapyPriorityQueue = self._mq()
        self.dqs: ScrapyPriorityQueue | None = self._dq() if self.dqdir else None
        return self.df.open()

    def close(self, reason: str) -> Deferred[None] | None:
        """
        (1) dump pending requests to disk if there is a disk queue
        (2) return the result of the dupefilter's ``close`` method
        """
        if self.dqs is not None:
            state = self.dqs.close()
            assert isinstance(self.dqdir, str)
            self._write_dqs_state(self.dqdir, state)
        return self.df.close(reason)

    def enqueue_request(self, request: Request) -> bool:
        """
        Unless the received request is filtered out by the Dupefilter, attempt to push
        it into the disk queue, falling back to pushing it into the memory queue.

        Increment the appropriate stats, such as: ``scheduler/enqueued``,
        ``scheduler/enqueued/disk``, ``scheduler/enqueued/memory``.

        Return ``True`` if the request was stored successfully, ``False`` otherwise.
        """
        if not request.dont_filter and self.df.request_seen(request):
            self.df.log(request, self.spider)
            return False
        dqok = self._dqpush(request)
        assert self.stats is not None
        if dqok:
            self.stats.inc_value("scheduler/enqueued/disk")
        else:
            self._mqpush(request)
            self.stats.inc_value("scheduler/enqueued/memory")
        self.stats.inc_value("scheduler/enqueued")
        return True

    def next_request(self) -> Request | None:
        """
        Return a :class:`~scrapy.Request` object from the memory queue,
        falling back to the disk queue if the memory queue is empty.
        Return ``None`` if there are no more enqueued requests.

        Increment the appropriate stats, such as: ``scheduler/dequeued``,
        ``scheduler/dequeued/disk``, ``scheduler/dequeued/memory``.
        """
        request: Request | None = self.mqs.pop()
        assert self.stats is not None
        if request is not None:
            self.stats.inc_value("scheduler/dequeued/memory")
        else:
            request = self._dqpop()
            if request is not None:
                self.stats.inc_value("scheduler/dequeued/disk")
        if request is not None:
            self.stats.inc_value("scheduler/dequeued")
        return request

    def __len__(self) -> int:
        """
        Return the total amount of enqueued requests
        """
        return len(self.dqs) + len(self.mqs) if self.dqs is not None else len(self.mqs)

    def _dqpush(self, request: Request) -> bool:
        if self.dqs is None:
            return False
        try:
            self.dqs.push(request)
        except ValueError as e:  # non serializable request
            if self.logunser:
                msg = (
                    "Unable to serialize request: %(request)s - reason:"
                    " %(reason)s - no more unserializable requests will be"
                    " logged (stats being collected)"
                )
                logger.warning(
                    msg,
                    {"request": request, "reason": e},
                    exc_info=True,
                    extra={"spider": self.spider},
                )
                self.logunser = False
            assert self.stats is not None
            self.stats.inc_value("scheduler/unserializable")
            return False
        return True

    def _mqpush(self, request: Request) -> None:
        self.mqs.push(request)

    def _dqpop(self) -> Request | None:
        if self.dqs is not None:
            return self.dqs.pop()
        return None

    def _mq(self) -> ScrapyPriorityQueue:
        """Create a new priority queue instance, with in-memory storage"""
        assert self.crawler
        assert self.pqclass
        return build_from_crawler(
            self.pqclass,
            self.crawler,
            downstream_queue_cls=self.mqclass,
            key="",
        )

    def _dq(self) -> ScrapyPriorityQueue:
        """Create a new priority queue instance, with disk storage"""
        assert self.crawler
        assert self.dqdir
        assert self.pqclass
        state = self._read_dqs_state(self.dqdir)
        q = build_from_crawler(
            self.pqclass,
            self.crawler,
            downstream_queue_cls=self.dqclass,
            key=self.dqdir,
            startprios=state,
        )
        if q:
            logger.info(
                "Resuming crawl (%(queuesize)d requests scheduled)",
                {"queuesize": len(q)},
                extra={"spider": self.spider},
            )
        return q

    def _dqdir(self, jobdir: str | None) -> str | None:
        """Return a folder name to keep disk queue state at"""
        if jobdir:
            dqdir = Path(jobdir, "requests.queue")
            if not dqdir.exists():
                dqdir.mkdir(parents=True)
            return str(dqdir)
        return None

    def _read_dqs_state(self, dqdir: str) -> list[int]:
        path = Path(dqdir, "active.json")
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as f:
            return cast(list[int], json.load(f))

    def _write_dqs_state(self, dqdir: str, state: list[int]) -> None:
        with Path(dqdir, "active.json").open("w", encoding="utf-8") as f:
            json.dump(state, f)
