/* Copyright 2023 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#ifndef THIRD_PARTY_PY_TENSORFLOW_DATA_VALIDATION_ANOMALIES_TELEMETRY_H_
#define THIRD_PARTY_PY_TENSORFLOW_DATA_VALIDATION_ANOMALIES_TELEMETRY_H_

#include "tensorflow_metadata/proto/v0/anomalies.pb.h"

namespace tensorflow {
namespace data_validation {

void UpdateTelemetry(const metadata::v0::Anomalies& result);

}  // namespace data_validation
}  // namespace tensorflow

#endif  // THIRD_PARTY_PY_TENSORFLOW_DATA_VALIDATION_ANOMALIES_TELEMETRY_H_
