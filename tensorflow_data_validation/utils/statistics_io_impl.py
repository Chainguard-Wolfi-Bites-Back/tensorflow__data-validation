# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License
"""Record sink support."""

from typing import Iterable, Iterator, Optional

import apache_beam as beam
import tensorflow as tf

from tensorflow_metadata.proto.v0 import statistics_pb2


class StatisticsIOProvider(object):
  """Provides access to read and write statistics proto to record files."""

  def record_sink_impl(self,
                       output_path_prefix: str) -> beam.PTransform:
    """Gets a beam IO sink for writing sharded statistics protos."""
    raise NotImplementedError

  def record_iterator_impl(
      self,
      paths: Optional[Iterable[str]] = None,
  ) -> Iterator[statistics_pb2.DatasetFeatureStatisticsList]:
    """Get a file-backed iterator over sharded statistics protos.

    Args:
      paths: A list of file paths containing statistics records.
    """
    raise NotImplementedError

  def glob(self, output_path_prefix: str) -> Iterator[str]:
    """Return files matching the pattern produced by record_sink_impl."""
    raise NotImplementedError


def get_io_provider(
    file_format: Optional[str] = None) -> StatisticsIOProvider:
  """Get a StatisticsIOProvider for writing and reading sharded stats.

  Args:
    file_format: Optional file format. Supports only tfrecords. If unset,
      defaults to tfrecords.

  Returns:
    A StatisticsIOProvider.
  """

  if file_format is None:
    file_format = 'tfrecords'
  if file_format not in ('tfrecords',):
    raise ValueError('Unrecognized file_format %s' % file_format)
  return _ProviderImpl(file_format)


class _ProviderImpl(StatisticsIOProvider):
  """TFRecord backed impl."""

  def __init__(self, file_format: str):
    self._file_format = file_format

  def record_sink_impl(self,
                       output_path_prefix: str,
                       file_format: Optional[str] = None) -> beam.PTransform:
    if self._file_format == 'tfrecords':
      return beam.io.WriteToTFRecord(
          output_path_prefix,
          coder=beam.coders.ProtoCoder(
              statistics_pb2.DatasetFeatureStatisticsList))
    else:
      raise ValueError(
          'Unrecognized file format %s.' % file_format)

  def glob(self, output_path_prefix) -> Iterator[str]:
    """Returns filenames matching the output pattern of record_sink_impl."""
    return tf.io.gfile.glob(output_path_prefix + '-*-of-*')

  def record_iterator_impl(
      self,
      paths: Iterable[str],
  ) -> Iterator[statistics_pb2.DatasetFeatureStatisticsList]:
    """Provides iterators over tfrecord backed statistics."""
    if self._file_format == 'tfrecords':
      iter_fn = tf.compat.v1.io.tf_record_iterator
    else:
      raise NotImplementedError('Unrecognized file_format %s' %
                                self._file_format)
    for path in paths:
      for record in iter_fn(path):
        stats_shard = statistics_pb2.DatasetFeatureStatisticsList()
        stats_shard.ParseFromString(record)
        yield stats_shard


def test_runner_impl():
  """Beam runner for testing record_sink_impl.

  Usage:

  with beam.Pipeline(runner=test_runner_impl()):
     ...

  Defined only for tests.

  Returns:
    A beam runner.
  """
  return None  # default runner.