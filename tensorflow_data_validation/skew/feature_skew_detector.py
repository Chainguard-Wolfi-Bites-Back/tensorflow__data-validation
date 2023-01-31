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
# limitations under the License.
"""Finds feature skew between baseline and test examples.

Feature skew is detected by joining baseline and test examples on a
fingerprint computed based on the provided identifier features. For each pair,
the feature skew detector compares the fingerprint of each baseline feature
value to the fingerprint of the corresponding test feature value.

If there is a mismatch in feature values, if the feature is only in the baseline
example, or if the feature is only in the test example, feature skew is
reported in the skew results and (optionally) a skew sample is output with
baseline-test example pairs that exhibit the feature skew.

For example, given the following examples with an identifier feature of 'id':
Baseline
  features {
    feature {
      key: "id"
      value { bytes_list {
        value: "id_1"
      }
    }
    feature {
      key: "float_values"
      value { float_list {
        value: 1.0
        value: 2.0
      }}
    }
  }

Test
  features {
    feature {
      key: "id"
      value { bytes_list {
        value: "id_1"
      }
    }
    feature {
      key: "float_values"
      value { float_list {
        value: 1.0
        value: 3.0
      }}
    }
  }

The following feature skew will be detected:
  feature_name: "float_values"
  baseline_count: 1
  test_count: 1
  mismatch_count: 1
  diff_count: 1


In addition to feature level skew information, the pipeline will also produce
overall metadata describing information about the matching process. See
feature_skew_results_pb2.MatchStats.

Confusion counts can also be generated by passing a list of ConfusionConfig
objects specifying features for analysis. If enabled for a feature, the output
will include a collection of counts in the form (base-value, test-value, count).
This represents a count across baseline feature value and test feature value
tuples for matched (by id) examples.
"""

from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union

import apache_beam as beam
import farmhash
import numpy as np
import tensorflow as tf
from tensorflow_data_validation import constants
from tensorflow_data_validation import types
from tensorflow_data_validation.utils import artifacts_io_impl
from tensorflow_data_validation.skew.protos import feature_skew_results_pb2


_BASELINE_KEY = "base"
_TEST_KEY = "test"

SKEW_RESULTS_KEY = "skew_results"
SKEW_PAIRS_KEY = "skew_pairs"
CONFUSION_KEY = "confusion_pairs"
MATCH_STATS_KEY = "match_stats"
_KEYED_EXAMPLE_KEY = "keyed_example"
_MISSING_IDS_KEY = "missing_ids"

_EXAMPLES_WITH_MISSING_IDENTIFIER_COUNTER = beam.metrics.Metrics.counter(
    constants.METRICS_NAMESPACE, "examples_with_missing_identifier_features")

_PerFeatureSkew = List[Tuple[str, feature_skew_results_pb2.FeatureSkew]]
_PairOrFeatureSkew = Union[feature_skew_results_pb2.SkewPair,
                           Tuple[str, feature_skew_results_pb2.FeatureSkew]]


def _get_serialized_feature(feature: tf.train.Feature,
                            float_round_ndigits: Optional[int]) -> str:
  """Gets serialized feature, rounding floats as specified.

  Args:
    feature: The feature to serialize.
    float_round_ndigits: Number of digits of precision after the decimal point
      to which to round float values before serializing the feature.

  Returns:
    The serialized feature.
  """
  kind = feature.WhichOneof("kind")
  if (kind == "bytes_list" or kind == "int64_list"):
    return str(feature.SerializePartialToString(deterministic=True))
  elif kind == "float_list":
    if float_round_ndigits is None:
      return str(feature.SerializePartialToString(deterministic=True))
    else:
      rounded_feature = tf.train.Feature()
      for value in feature.float_list.value:
        rounded_feature.float_list.value.append(
            round(value, float_round_ndigits))
      return str(rounded_feature.SerializePartialToString(deterministic=True))
  else:
    raise ValueError("Unknown feature type detected: %s" % kind)


def _compute_skew_for_features(
    base_feature: tf.train.Feature, test_feature: tf.train.Feature,
    float_round_ndigits: Optional[int],
    feature_name: str) -> feature_skew_results_pb2.FeatureSkew:
  """Computes feature skew for a pair of baseline and test features.

  Args:
    base_feature: The feature to compare from the baseline example.
    test_feature: The feature to compare from the test example.
    float_round_ndigits: Number of digits precision after the decimal point to
      which to round float values before comparison.
    feature_name: The name of the feature for which to compute skew between the
      examples.

  Returns:
    A FeatureSkew proto containing information about skew for the specified
      feature.
  """
  skew_results = feature_skew_results_pb2.FeatureSkew()
  skew_results.feature_name = feature_name
  if not _empty_or_null(base_feature) and not _empty_or_null(test_feature):
    skew_results.base_count = 1
    skew_results.test_count = 1
    if (farmhash.fingerprint64(
        _get_serialized_feature(base_feature,
                                float_round_ndigits)) == farmhash.fingerprint64(
                                    _get_serialized_feature(
                                        test_feature, float_round_ndigits))):
      skew_results.match_count = 1
    else:
      skew_results.mismatch_count = 1
  elif not _empty_or_null(base_feature):
    skew_results.base_count = 1
    skew_results.base_only = 1
  elif not _empty_or_null(test_feature):
    skew_results.test_count = 1
    skew_results.test_only = 1
  elif (test_feature is None) == (base_feature is None):
    # Both features are None, or present with zero values.
    skew_results.match_count = 1
  return skew_results


def _compute_skew_for_examples(
    base_example: tf.train.Example, test_example: tf.train.Example,
    features_to_ignore: List[tf.train.Feature],
    float_round_ndigits: Optional[int]) -> Tuple[_PerFeatureSkew, bool]:
  """Computes feature skew for a pair of baseline and test examples.

  Args:
    base_example: The baseline example to compare.
    test_example: The test example to compare.
    features_to_ignore: The features not to compare.
    float_round_ndigits: Number of digits precision after the decimal point to
      which to round float values before comparison.

  Returns:
    A tuple containing a list of the skew information for each feature
    and a boolean indicating whether skew was found in any feature, in which
    case the examples are considered skewed.
  """
  all_feature_names = set()
  all_feature_names.update(base_example.features.feature.keys())
  all_feature_names.update(test_example.features.feature.keys())
  feature_names = all_feature_names.difference(set(features_to_ignore))

  result = list()
  is_skewed = False
  for name in feature_names:
    base_feature = base_example.features.feature.get(name)
    test_feature = test_example.features.feature.get(name)
    skew = _compute_skew_for_features(base_feature, test_feature,
                                      float_round_ndigits, name)
    if skew.match_count == 0:
      # If any features have a mismatch or are found only in the baseline or
      # test example, the examples are considered skewed.
      is_skewed = True
    result.append((name, skew))
  return result, is_skewed


def _merge_feature_skew_results(
    skew_results: Iterable[feature_skew_results_pb2.FeatureSkew]
) -> feature_skew_results_pb2.FeatureSkew:
  """Merges multiple FeatureSkew protos into a single FeatureSkew proto.

  Args:
    skew_results: An iterable of FeatureSkew protos.

  Returns:
    A FeatureSkew proto containing the result of merging the inputs.
  """
  result = feature_skew_results_pb2.FeatureSkew()
  for skew_result in skew_results:
    if not result.feature_name:
      result.feature_name = skew_result.feature_name
    elif result.feature_name != skew_result.feature_name:
      raise ValueError("Attempting to merge skew results with different names.")
    result.base_count += skew_result.base_count
    result.test_count += skew_result.test_count
    result.match_count += skew_result.match_count
    result.base_only += skew_result.base_only
    result.test_only += skew_result.test_only
    result.mismatch_count += skew_result.mismatch_count
  result.diff_count = (
      result.base_only + result.test_only + result.mismatch_count)
  return result


def _construct_skew_pair(
    per_feature_skew: List[Tuple[str, feature_skew_results_pb2.FeatureSkew]],
    base_example: tf.train.Example,
    test_example: tf.train.Example) -> feature_skew_results_pb2.SkewPair:
  """Constructs a SkewPair from baseline and test examples.

  Args:
    per_feature_skew: Skew results for each feature in the input examples.
    base_example: The baseline example to include.
    test_example: The test example to include.

  Returns:
    A SkewPair containing examples that exhibit some skew.
  """
  skew_pair = feature_skew_results_pb2.SkewPair()
  skew_pair.base.CopyFrom(base_example)
  skew_pair.test.CopyFrom(test_example)

  for feature_name, skew_result in per_feature_skew:
    if skew_result.match_count == 1:
      skew_pair.matched_features.append(feature_name)
    elif skew_result.base_only == 1:
      skew_pair.base_only_features.append(feature_name)
    elif skew_result.test_only == 1:
      skew_pair.test_only_features.append(feature_name)
    elif skew_result.mismatch_count == 1:
      skew_pair.mismatched_features.append(feature_name)

  return skew_pair


def _empty_or_null(feature: Optional[tf.train.Feature]) -> bool:
  """True if feature is None or holds no values."""
  if feature is None:
    return True
  if len(feature.bytes_list.value) + len(feature.int64_list.value) + len(
      feature.float_list.value) == 0:
    return True
  return False


class _ExtractIdentifiers(beam.DoFn):
  """DoFn that extracts a unique fingerprint for each example.

  This class computes fingerprints by combining the identifier features.
  """

  def __init__(self, identifier_features: List[types.FeatureName],
               float_round_ndigits: Optional[int]) -> None:
    """Initializes _ExtractIdentifiers.

    Args:
      identifier_features: The names of the features to use to compute a
        fingerprint for the example.
      float_round_ndigits: Number of digits precision after the decimal point to
        which to round float values before generating the fingerprint.
    """
    self._identifier_features = sorted(identifier_features)
    self._float_round_ndigits = float_round_ndigits

  def process(self, example: tf.train.Example):
    serialized_feature_values = []
    for identifier_feature in self._identifier_features:
      feature = example.features.feature.get(identifier_feature)
      if _empty_or_null(feature):
        _EXAMPLES_WITH_MISSING_IDENTIFIER_COUNTER.inc()
        yield beam.pvalue.TaggedOutput(_MISSING_IDS_KEY, 1)
        return
      else:
        serialized_feature_values.append(
            _get_serialized_feature(feature, self._float_round_ndigits))
    keyed_example = (str(
        farmhash.fingerprint64("".join(serialized_feature_values))), example)
    yield beam.pvalue.TaggedOutput(_KEYED_EXAMPLE_KEY, keyed_example)


class ConfusionConfig(object):
  """Configures confusion analysis."""

  def __init__(self, name: types.FeatureName):
    self.name = name


_ConfusionFeatureValue = bytes


_MISSING_VALUE_PLACEHOLDER = b"__MISSING_VALUE__"


def _get_confusion_feature_value(
    ex: tf.train.Example,
    name: types.FeatureName) -> Optional[_ConfusionFeatureValue]:
  """Returns a value for a named feature for confusion analysis."""
  f = ex.features.feature.get(name, None)
  if f is None:
    return _MISSING_VALUE_PLACEHOLDER
  if f.int64_list.value:
    raise ValueError("int64 features unsupported for confusion analysis.")
  if f.float_list.value:
    raise ValueError("float features unsupported for confusion analysis.")
  if len(f.bytes_list.value) > 1:
    raise ValueError("multivalent features unsupported for confusion analysis.")
  if not f.bytes_list.value:
    return _MISSING_VALUE_PLACEHOLDER
  return f.bytes_list.value[0]


def _yield_confusion_pairs(
    ex_base: tf.train.Example, ex_test: tf.train.Example,
    configs: List[ConfusionConfig]
) -> Iterator[Tuple[_ConfusionFeatureValue, _ConfusionFeatureValue,
                    types.FeatureName]]:
  """Yield base/test value pairs from a matching pair of examples."""
  for config in configs:
    base_val = _get_confusion_feature_value(ex_base, config.name)
    test_val = _get_confusion_feature_value(ex_test, config.name)
    if base_val is not None and test_val is not None:
      yield base_val, test_val, config.name


def _confusion_count_to_proto(
    values_count: Tuple[Tuple[_ConfusionFeatureValue, _ConfusionFeatureValue,
                              types.FeatureName], int]
) -> feature_skew_results_pb2.ConfusionCount:
  """Convert a confusion count tuple and count to string."""
  (base_val, test_val, feature_name), count = values_count
  cc = feature_skew_results_pb2.ConfusionCount(
      feature_name=feature_name, count=count)
  cc.base.bytes_value = base_val
  cc.test.bytes_value = test_val
  return cc


def _make_match_stats_counter(base_with_id_count=0,
                              test_with_id_count=0,
                              id_count=0,
                              missing_base_count=0,
                              missing_test_count=0,
                              pairs_count=0,
                              duplicate_id_count=0,
                              ids_missing_in_base_count=0,
                              ids_missing_in_test_count=0) -> np.ndarray:
  return np.array([
      base_with_id_count, test_with_id_count, id_count, missing_base_count,
      missing_test_count, pairs_count, duplicate_id_count,
      ids_missing_in_base_count, ids_missing_in_test_count
  ],
                  dtype=np.int64)


class _MergeMatchStatsFn(beam.CombineFn):
  """CombineFn to generate MatchStats."""

  def create_accumulator(self):
    return _make_match_stats_counter()

  def add_input(self, mutable_accumulator: np.ndarray,
                element: np.ndarray) -> np.ndarray:
    mutable_accumulator += element
    return mutable_accumulator

  def merge_accumulators(self,
                         accumulators: Iterable[np.ndarray]) -> np.ndarray:
    it = iter(accumulators)
    acc = next(it)
    for a in it:
      acc += a
    return acc

  def extract_output(
      self, accumulator: np.ndarray) -> feature_skew_results_pb2.MatchStats:
    return feature_skew_results_pb2.MatchStats(
        base_with_id_count=accumulator[0],
        test_with_id_count=accumulator[1],
        identifiers_count=accumulator[2],
        ids_missing_in_base_count=accumulator[3],
        ids_missing_in_test_count=accumulator[4],
        matching_pairs_count=accumulator[5],
        duplicate_id_count=accumulator[6],
        base_missing_id_count=accumulator[7],
        test_missing_id_count=accumulator[8])


class _ComputeSkew(beam.DoFn):
  """DoFn that computes skew for each pair of examples."""

  def __init__(self, features_to_ignore: List[tf.train.Feature],
               float_round_ndigits: Optional[int], allow_duplicate_identifiers,
               confusion_configs: List[ConfusionConfig]) -> None:
    """Initializes _ComputeSkew.

    Args:
      features_to_ignore: Names of features that are ignored in skew detection.
      float_round_ndigits: Number of digits precision after the decimal point to
        which to round float values before detecting skew.
      allow_duplicate_identifiers: If set, skew detection will be done on
        examples for which there are duplicate identifier feature values. In
        this case, the counts in the FeatureSkew result are based on each
        baseline-test example pair analyzed. Examples with given identifier
        feature values must all fit in memory.
      confusion_configs: Optional list of ConfusionConfig objects describing
        per-feature config for value confusion analysis.
    """
    self._features_to_ignore = features_to_ignore
    self._float_round_ndigits = float_round_ndigits
    self._allow_duplicate_identifiers = allow_duplicate_identifiers
    self._skipped_duplicate_identifiers_counter = beam.metrics.Metrics.counter(
        constants.METRICS_NAMESPACE, "examplediff_skip_dupe_id")
    self._ids_counter = beam.metrics.Metrics.counter(
        constants.METRICS_NAMESPACE, "examplediff_ids_counter")
    self._pairs_counter = beam.metrics.Metrics.counter(
        constants.METRICS_NAMESPACE, "examplediff_pairs_counter")
    self._confusion_configs = confusion_configs

  def process(
      self, element: Tuple[str, Dict[str, Iterable[Any]]]
  ) -> Iterable[_PairOrFeatureSkew]:
    (_, examples) = element
    base_examples = list(examples.get(_BASELINE_KEY))
    test_examples = list(examples.get(_TEST_KEY))

    match_stats = _make_match_stats_counter(
        len(base_examples),
        len(test_examples),
        1,
        0 if base_examples else 1,
        0 if test_examples else 1,
        len(base_examples) * len(test_examples),
        1 if len(base_examples) > 1 or len(test_examples) > 1 else 0,
    )
    yield beam.pvalue.TaggedOutput(MATCH_STATS_KEY, match_stats)
    self._ids_counter.inc(1)
    self._pairs_counter.inc(len(base_examples) * len(test_examples))
    if not self._allow_duplicate_identifiers:
      if len(base_examples) > 1 or len(test_examples) > 1:
        self._skipped_duplicate_identifiers_counter.inc(1)
        return
    if base_examples and test_examples:
      for base_example in base_examples:
        for test_example in test_examples:
          result, is_skewed = _compute_skew_for_examples(
              base_example, test_example, self._features_to_ignore,
              self._float_round_ndigits)
          if is_skewed:
            skew_pair = _construct_skew_pair(result, base_example,
                                             test_example)
            yield beam.pvalue.TaggedOutput(SKEW_PAIRS_KEY, skew_pair)
          for each in result:
            yield beam.pvalue.TaggedOutput(SKEW_RESULTS_KEY, each)
          if self._confusion_configs is not None:
            for pair in _yield_confusion_pairs(base_example, test_example,
                                               self._confusion_configs):
              yield beam.pvalue.TaggedOutput(CONFUSION_KEY, pair)


def _extract_compute_skew_result(
    results: beam.pvalue.DoOutputsTuple
) -> Tuple[beam.PCollection[Tuple[str, feature_skew_results_pb2.FeatureSkew]],
           beam.PCollection[feature_skew_results_pb2.SkewPair],
           beam.PCollection[np.ndarray], Optional[beam.PCollection[Tuple[
               _ConfusionFeatureValue, _ConfusionFeatureValue, str]]]]:
  """Extracts results of _ComputeSkew and fixes type hints."""

  # Fix output type hints.
  # TODO(b/211806179): Revert this hack.
  results_skew_results = (
      results[SKEW_RESULTS_KEY]
      | "FixSkewResultsTypeHints" >> beam.Map(lambda x: x).with_output_types(
          Tuple[str, feature_skew_results_pb2.FeatureSkew]))
  results_skew_pairs = (
      results[SKEW_PAIRS_KEY]
      | "FixSkewPairsTypeHints" >> beam.Map(lambda x: x).with_output_types(
          feature_skew_results_pb2.SkewPair))
  results_match_stats = (
      results[MATCH_STATS_KEY]
      | "FixMatchStatsTypeHints" >> beam.Map(lambda x: x).with_output_types(
          np.ndarray))
  try:
    results_confusion_tuples = (
        results[CONFUSION_KEY]
        | "FixConfusionTypeHints" >> beam.Map(lambda x: x).with_output_types(
            Tuple[_ConfusionFeatureValue, _ConfusionFeatureValue, str]))
  except ValueError:
    results_confusion_tuples = None
  return (results_skew_results, results_skew_pairs, results_match_stats,
          results_confusion_tuples)


def _extract_extract_identifiers_result(
    results_base: beam.pvalue.DoOutputsTuple,
    results_test: beam.pvalue.DoOutputsTuple
) -> Tuple[beam.PCollection[Tuple[str, tf.train.Example]],
           beam.PCollection[np.ndarray], beam.PCollection[Tuple[
               str, tf.train.Example]], beam.PCollection[np.ndarray]]:
  """Extracts results of _ExtractIdentifiers and fixes type hints."""

  keyed_base_examples = (
      results_base[_KEYED_EXAMPLE_KEY] | "FixKeyedBaseType" >>
      beam.Map(lambda x: x).with_output_types(Tuple[str, tf.train.Example]))
  missing_id_base_examples = (
      results_base[_MISSING_IDS_KEY]
      | "BaseMissingCountsToMatchCounter" >>
      beam.Map(lambda x: _make_match_stats_counter(ids_missing_in_base_count=x))
  )

  keyed_test_examples = (
      results_test[_KEYED_EXAMPLE_KEY] | "FixKeyedTestType" >>
      beam.Map(lambda x: x).with_output_types(Tuple[str, tf.train.Example]))
  missing_id_test_examples = (
      results_test[_MISSING_IDS_KEY]
      | "TestMissingCountsToMatchCounter" >>
      beam.Map(lambda x: _make_match_stats_counter(ids_missing_in_test_count=x))
  )

  return (keyed_base_examples, missing_id_base_examples, keyed_test_examples,
          missing_id_test_examples)


class DetectFeatureSkewImpl(beam.PTransform):
  """Identifies feature skew in baseline and test examples.

  This PTransform returns a dict of PCollections containing:
    SKEW_RESULTS_KEY: Aggregated skew statistics (containing, e.g., mismatch
      count, baseline only, test only) for each feature; and
    SKEW_PAIRS_KEY: A sample of skewed example pairs (if sample_size is > 0).
    MATCH_STATS: A PCollection containing a single MatchStats proto.
    CONFUSION_KEY: (if configured) counts of paired feature values.
  """

  def __init__(
      self,
      identifier_features: List[types.FeatureName],
      features_to_ignore: Optional[List[types.FeatureName]] = None,
      sample_size: int = 0,
      float_round_ndigits: Optional[int] = None,
      allow_duplicate_identifiers: bool = False,
      confusion_configs: Optional[List[ConfusionConfig]] = None) -> None:
    """Initializes DetectFeatureSkewImpl.

    Args:
      identifier_features: The names of the features to use to identify an
        example.
      features_to_ignore: The names of the features for which skew detection is
        not done.
      sample_size: Size of the sample of baseline-test example pairs that
        exhibit skew to include in the skew results.
      float_round_ndigits: Number of digits of precision after the decimal point
        to which to round float values before detecting skew.
      allow_duplicate_identifiers: If set, skew detection will be done on
        examples for which there are duplicate identifier feature values. In
        this case, the counts in the FeatureSkew result are based on each
        baseline-test example pair analyzed. Examples with given identifier
        feature values must all fit in memory.
      confusion_configs: Optional list of ConfusionConfig objects describing
        per-feature config for value confusion analysis. If provided, the result
        will contain a value keyed under CONFUSION_KEY containing a PCollection
        of ConfusionCount protos.
    """
    if not identifier_features:
      raise ValueError("At least one feature name must be specified in "
                       "identifier_features.")
    self._identifier_features = identifier_features
    self._sample_size = sample_size
    self._float_round_ndigits = float_round_ndigits
    if features_to_ignore is not None:
      self._features_to_ignore = features_to_ignore + identifier_features
    else:
      self._features_to_ignore = identifier_features
    self._allow_duplicate_identifiers = allow_duplicate_identifiers
    self._confusion_configs = ([] if confusion_configs is None else
                               confusion_configs)

  def expand(
      self, pcollections: Tuple[beam.pvalue.PCollection,
                                beam.pvalue.PCollection]
  ) -> Dict[str, beam.pvalue.PCollection]:
    base_examples, test_examples = pcollections
    # Extract keyed base examples and counts of missing keys.
    keyed_base_examples_result = (
        base_examples | "ExtractBaseIdentifiers" >> beam.ParDo(
            _ExtractIdentifiers(self._identifier_features,
                                self._float_round_ndigits)).with_outputs(
                                    _KEYED_EXAMPLE_KEY, _MISSING_IDS_KEY))

    # Extract keyed test examples and counts of missing keys.
    keyed_test_examples_result = (
        test_examples | "ExtractTestIdentifiers" >> beam.ParDo(
            _ExtractIdentifiers(self._identifier_features,
                                self._float_round_ndigits)).with_outputs(
                                    _KEYED_EXAMPLE_KEY, _MISSING_IDS_KEY))
    (keyed_base_examples, missing_id_base_examples, keyed_test_examples,
     missing_id_test_examples) = _extract_extract_identifiers_result(
         keyed_base_examples_result, keyed_test_examples_result)

    outputs = [SKEW_RESULTS_KEY, SKEW_PAIRS_KEY, MATCH_STATS_KEY]
    if self._confusion_configs:
      outputs.append(CONFUSION_KEY)
    results = (
        {
            "base": keyed_base_examples,
            "test": keyed_test_examples
        } | "JoinExamples" >> beam.CoGroupByKey()
        | "ComputeSkew" >> beam.ParDo(
            _ComputeSkew(self._features_to_ignore, self._float_round_ndigits,
                         self._allow_duplicate_identifiers,
                         self._confusion_configs)).with_outputs(*outputs))
    (results_skew_results, results_skew_pairs, results_match_stats,
     results_confusion_tuples) = _extract_compute_skew_result(results)

    outputs = {}
    # Merge skew results.
    skew_results = (
        results_skew_results
        | "MergeSkewResultsPerFeature" >>  # pytype: disable=attribute-error
        beam.CombinePerKey(_merge_feature_skew_results)
        | "DropKeys" >> beam.Values())
    outputs[SKEW_RESULTS_KEY] = skew_results

    # Merge match stats.
    match_stats = (
        [
            results_match_stats, missing_id_test_examples,
            missing_id_base_examples
        ]
        | "FlattenMatchStats" >> beam.Flatten()
        | "MergeMatchStats" >> beam.CombineGlobally(_MergeMatchStatsFn()))
    outputs[MATCH_STATS_KEY] = match_stats

    # Sample skew pairs.
    skew_pairs = (
        results_skew_pairs | "SampleSkewPairs" >>  # pytype: disable=attribute-error
        beam.combiners.Sample.FixedSizeGlobally(self._sample_size)
        # Sampling results in a pcollection with a single element consisting of
        # a list of the samples. Convert this to a pcollection of samples.
        | "Flatten" >> beam.FlatMap(lambda x: x))
    outputs[SKEW_PAIRS_KEY] = skew_pairs
    if results_confusion_tuples is not None:
      confusion_counts = (
          results_confusion_tuples
          | "CountConfusion" >> beam.combiners.Count.PerElement()
          | "MakeConfusionProto" >> beam.Map(_confusion_count_to_proto))
      outputs[CONFUSION_KEY] = confusion_counts
    return outputs


def skew_results_sink(output_path_prefix: str) -> beam.PTransform:
  """Record based PSink for FeatureSkew protos."""
  return artifacts_io_impl.feature_skew_sink(
      output_path_prefix,
      feature_skew_results_pb2.FeatureSkew)


def skew_pair_sink(output_path_prefix: str) -> beam.PTransform:
  """Record based PSink for SkewPair protos."""
  return artifacts_io_impl.feature_skew_sink(
      output_path_prefix,
      feature_skew_results_pb2.SkewPair)


def confusion_count_sink(output_path_prefix: str) -> beam.PTransform:
  """Record based PSink for ConfusionCount protos."""
  return artifacts_io_impl.feature_skew_sink(
      output_path_prefix,
      feature_skew_results_pb2.ConfusionCount)


def match_stats_sink(output_path_prefix: str) -> beam.PTransform:
  """Record based PSink for MatchStats protos."""
  return artifacts_io_impl.feature_skew_sink(
      output_path_prefix,
      feature_skew_results_pb2.MatchStats)


def skew_results_iterator(
    input_pattern_prefix) -> Iterator[feature_skew_results_pb2.FeatureSkew]:
  """Reads records written by skew_results_sink."""
  return artifacts_io_impl.default_record_reader(
      input_pattern_prefix + "*-of-*", feature_skew_results_pb2.FeatureSkew)


def skew_pair_iterator(
    input_pattern_prefix) -> Iterator[feature_skew_results_pb2.SkewPair]:
  """Reads records written by skew_pair_sink."""
  return artifacts_io_impl.default_record_reader(
      input_pattern_prefix + "*-of-*", feature_skew_results_pb2.SkewPair)


def match_stats_iterator(
    input_pattern_prefix) -> Iterator[feature_skew_results_pb2.MatchStats]:
  """Reads records written by match_stats_sink."""
  return artifacts_io_impl.default_record_reader(
      input_pattern_prefix + "*-of-*", feature_skew_results_pb2.MatchStats)


def confusion_count_iterator(
    input_pattern_prefix) -> Iterator[feature_skew_results_pb2.ConfusionCount]:
  """Reads records written by confusion_count_sink."""
  return artifacts_io_impl.default_record_reader(
      input_pattern_prefix + "*-of-*", feature_skew_results_pb2.ConfusionCount)
