# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os
import shutil
import sys
from hashlib import sha1

from pants.build_graph.build_graph import sort_targets
from pants.build_graph.target import Target
from pants.invalidation.build_invalidator import BuildInvalidator, CacheKeyGenerator
from pants.util.dirutil import safe_mkdir


class VersionedTargetSet(object):
  """Represents a list of targets, a corresponding CacheKey, and a flag determining whether the
  list of targets is currently valid.

  When invalidating a single target, this can be used to represent that target as a singleton.
  When checking the artifact cache, this can also be used to represent a list of targets that are
  built together into a single artifact.
  """

  @staticmethod
  def from_versioned_targets(versioned_targets):
    first_target = versioned_targets[0]
    cache_manager = first_target._cache_manager

    # Quick sanity check; all the versioned targets should have the same cache manager.
    # TODO(ryan): the way VersionedTargets store their own links to a single CacheManager instance
    # feels hacky; see if there's a cleaner way for callers to handle awareness of the CacheManager.
    for versioned_target in versioned_targets:
      if versioned_target._cache_manager != cache_manager:
        raise ValueError("Attempting to combine versioned targets {} and {} with different"
                         " CacheManager instances: {} and {}".format(first_target, versioned_target,
                                                                 cache_manager,
                                                                 versioned_target._cache_manager))
    return VersionedTargetSet(cache_manager, versioned_targets)

  def __init__(self, cache_manager, versioned_targets):
    self._cache_manager = cache_manager
    self.versioned_targets = versioned_targets
    self.targets = [vt.target for vt in versioned_targets]

    # The following line is a no-op if cache_key was set in the VersionedTarget __init__ method.
    self.cache_key = CacheKeyGenerator.combine_cache_keys([vt.cache_key
                                                           for vt in versioned_targets])
    # NB: previous_cache_key may be None on the first build of a target.
    self.previous_cache_key = cache_manager.previous_key(self.cache_key)
    self.valid = self.previous_cache_key == self.cache_key

    self.num_chunking_units = self.cache_key.num_chunking_units
    if cache_manager.invalidation_report:
      cache_manager.invalidation_report.add_vts(cache_manager, self.targets, self.cache_key,
                                                self.valid, phase='init')

    self._results_dir = None
    self._previous_results_dir = None
    # True if the results_dir for this VT was created incrementally via clone of the
    # previous results_dir.
    self.is_incremental = False

  def update(self):
    self._cache_manager.update(self)

  def force_invalidate(self):
    self._cache_manager.force_invalidate(self)

  @property
  def has_results_dir(self):
    return self._results_dir is not None

  @property
  def results_dir(self):
    """The directory that stores results for this version of these targets."""
    if self._results_dir is None:
      raise ValueError('No results_dir was created for {}'.format(self))
    return self._results_dir

  @property
  def previous_results_dir(self):
    """The directory that stores results for the previous version of these targets.

    Only valid if is_incremental is true.

    TODO: Exposing old results is a bit of an abstraction leak, because ill-behaved Tasks could
    mutate them.
    """
    if self._previous_results_dir is None:
      raise ValueError('There is no previous_results_dir for: {}'.format(self))
    return self._previous_results_dir

  def __repr__(self):
    return 'VTS({}, {})'.format(','.join(target.address.spec for target in self.targets),
                                'valid' if self.valid else 'invalid')


class VersionedTarget(VersionedTargetSet):
  """This class represents a singleton VersionedTargetSet, and has links to VersionedTargets that
  the wrapped target depends on (after having resolved through any "alias" targets.
  """

  def __init__(self, cache_manager, target, cache_key):
    if not isinstance(target, Target):
      raise ValueError("The target {} must be an instance of Target but is not.".format(target.id))

    self.target = target
    self.cache_key = cache_key
    # Must come after the assignments above, as they are used in the parent's __init__.
    super(VersionedTarget, self).__init__(cache_manager, [self])
    self.id = target.id

  def create_results_dir(self, root_dir, allow_incremental):
    """Ensures that a results_dir exists under the given root_dir for this versioned target.

    If incremental=True, attempts to clone the results_dir for the previous version of this target
    to the new results dir. Otherwise, simply ensures that the results dir exists.
    """
    def dirname(key):
      def version_to_string(task_ver):
        return '.'.join(['_'.join(map(str, x)) for x in task_ver])
      task_version = version_to_string(self._cache_manager.task_version)
      # TODO: Shorten cache_key hashes in general?
      return os.path.join(
          root_dir,
          sha1(task_version).hexdigest()[:12],
          key.id,
          sha1(key.hash).hexdigest()[:12]
      )
    new_dir = dirname(self.cache_key)
    self._results_dir = new_dir
    if self.valid:
      return

    if allow_incremental and self.previous_cache_key:
      self.is_incremental = True
      old_dir = dirname(self.previous_cache_key)
      self._previous_results_dir = old_dir
      if os.path.isdir(old_dir) and not os.path.isdir(new_dir):
        shutil.copytree(old_dir, new_dir)
    else:
      safe_mkdir(new_dir)

  def __repr__(self):
    return 'VT({}, {})'.format(self.target.id, 'valid' if self.valid else 'invalid')


class InvalidationCheck(object):
  """The result of calling check() on a CacheManager.

  Each member is a list of VersionedTargetSet objects.  Sorting of the targets depends
  on how you order the InvalidationCheck from the InvalidationCacheManager.

  Tasks may need to perform no, some or all operations on either of these, depending on how they
  are implemented.
  """

  @classmethod
  def _partition_versioned_targets(cls, versioned_targets, partition_size_hint, vt_colors=None):
    """Groups versioned targets so that each group has roughly the same number of sources.

    versioned_targets is a list of VersionedTarget objects  [vt1, vt2, vt3, vt4, vt5, vt6, ...].

    Returns a list of VersionedTargetSet objects, e.g., [VT1, VT2, VT3, ...] representing the
    same underlying targets. E.g., VT1 is the combination of [vt1, vt2, vt3], VT2 is the combination
    of [vt4, vt5] and VT3 is [vt6].

    The new versioned targets are chosen to have roughly partition_size_hint sources.

    If vt_colors is specified, it must be a map from VersionedTarget -> opaque 'color' values.
    Two VersionedTargets will be in the same partition only if they have the same color.

    This is useful as a compromise between flat mode, where we build all targets in a
    single compiler invocation, and non-flat mode, where we invoke a compiler for each target,
    which may lead to lots of compiler startup overhead. A task can choose instead to build one
    group at a time.
    """
    res = []

    # Hack around the python outer scope problem.
    class VtGroup(object):

      def __init__(self):
        self.vts = []
        self.total_chunking_units = 0

    current_group = VtGroup()

    def add_to_current_group(vt):
      current_group.vts.append(vt)
      current_group.total_chunking_units += vt.num_chunking_units

    def close_current_group():
      if len(current_group.vts) > 0:
        new_vt = VersionedTargetSet.from_versioned_targets(current_group.vts)
        res.append(new_vt)
        current_group.vts = []
        current_group.total_chunking_units = 0

    current_color = None
    for vt in versioned_targets:
      if vt_colors:
        color = vt_colors.get(vt, current_color)
        if current_color is None:
          current_color = color
        if color != current_color:
          close_current_group()
          current_color = color
      add_to_current_group(vt)
      if current_group.total_chunking_units > 1.5 * partition_size_hint and len(current_group.vts) > 1:
        # Too big. Close the current group without this vt and add it to the next one.
        current_group.vts.pop()
        close_current_group()
        add_to_current_group(vt)
      elif current_group.total_chunking_units > partition_size_hint:
        close_current_group()
    close_current_group()  # Close the last group, if any.

    return res

  def __init__(self, all_vts, invalid_vts, partition_size_hint=None, target_colors=None):
    # target_colors is specified by Target. We need it by VersionedTarget.
    vt_colors = {}
    if target_colors:
      for vt in all_vts:
        if vt.target in target_colors:
          vt_colors[vt] = target_colors[vt.target]

    # All the targets, valid and invalid.
    self.all_vts = all_vts

    # All the targets, partitioned if so requested.
    self.all_vts_partitioned = \
      self._partition_versioned_targets(all_vts, partition_size_hint, vt_colors) \
        if (partition_size_hint or vt_colors) else all_vts

    # Just the invalid targets.
    self.invalid_vts = invalid_vts

    # Just the invalid targets, partitioned if so requested.
    self.invalid_vts_partitioned = \
      self._partition_versioned_targets(invalid_vts, partition_size_hint, vt_colors) \
        if (partition_size_hint or vt_colors) else invalid_vts


class InvalidationCacheManager(object):
  """Manages cache checks, updates and invalidation keeping track of basic change
  and invalidation statistics.
  Note that this is distinct from the ArtifactCache concept, and should probably be renamed.
  """

  class CacheValidationError(Exception):
    """Indicates a problem accessing the cache."""

  def __init__(self,
               cache_key_generator,
               build_invalidator_dir,
               invalidate_dependents,
               fingerprint_strategy=None,
               invalidation_report=None,
               task_name=None,
               task_version=None,):
    self._cache_key_generator = cache_key_generator
    self._task_name = task_name or 'UNKNOWN'
    self._task_version = task_version or 'Unknown_0'
    self._invalidate_dependents = invalidate_dependents
    self._invalidator = BuildInvalidator(build_invalidator_dir)
    self._fingerprint_strategy = fingerprint_strategy
    self.invalidation_report = invalidation_report

  def update(self, vts):
    """Mark a changed or invalidated VersionedTargetSet as successfully processed."""
    for vt in vts.versioned_targets:
      self._invalidator.update(vt.cache_key)
      vt.valid = True
    self._invalidator.update(vts.cache_key)
    vts.valid = True

  def force_invalidate(self, vts):
    """Force invalidation of a VersionedTargetSet."""
    for vt in vts.versioned_targets:
      self._invalidator.force_invalidate(vt.cache_key)
      vt.valid = False
    self._invalidator.force_invalidate(vts.cache_key)
    vts.valid = False

  def check(self,
            targets,
            partition_size_hint=None,
            target_colors=None,
            topological_order=False):
    """Checks whether each of the targets has changed and invalidates it if so.

    Returns a list of VersionedTargetSet objects (either valid or invalid). The returned sets
    'cover' the input targets, possibly partitioning them, with one caveat: if the FingerprintStrategy
    opted out of fingerprinting a target because it doesn't contribute to invalidation, then that
    target will be excluded from all_vts, invalid_vts, and the partitioned VTS.

    Callers can inspect these vts and rebuild the invalid ones, for example.

    If target_colors is specified, it must be a map from Target -> opaque 'color' values.
    Two Targets will be in the same partition only if they have the same color.
    """
    all_vts = self.wrap_targets(targets, topological_order=topological_order)
    invalid_vts = filter(lambda vt: not vt.valid, all_vts)
    return InvalidationCheck(all_vts, invalid_vts, partition_size_hint, target_colors)

  @property
  def task_name(self):
    return self._task_name

  @property
  def task_version(self):
    return self._task_version

  def wrap_targets(self, targets, topological_order=False):
    """Wrap targets and their computed cache keys in VersionedTargets.

    If the FingerprintStrategy opted out of providing a fingerprint for a target, that target will not
    have an associated VersionedTarget returned.

    Returns a list of VersionedTargets, each representing one input target.
    """
    def vt_iter():
      if topological_order:
        sorted_targets = [t for t in reversed(sort_targets(targets)) if t in targets]
      else:
        sorted_targets = sorted(targets)
      for target in sorted_targets:
        target_key = self._key_for(target)
        if target_key is not None:
          yield VersionedTarget(self, target, target_key)
    return list(vt_iter())

  def previous_key(self, cache_key):
    return self._invalidator.previous_key(cache_key)

  def _key_for(self, target):
    try:
      return self._cache_key_generator.key_for_target(target,
                                                      transitive=self._invalidate_dependents,
                                                      fingerprint_strategy=self._fingerprint_strategy)
    except Exception as e:
      # This is a catch-all for problems we haven't caught up with and given a better diagnostic.
      # TODO(Eric Ayers): If you see this exception, add a fix to catch the problem earlier.
      exc_info = sys.exc_info()
      new_exception = self.CacheValidationError("Problem validating target {} in {}: {}"
                                                .format(target.id, target.address.spec_path, e))

      raise self.CacheValidationError, new_exception, exc_info[2]
