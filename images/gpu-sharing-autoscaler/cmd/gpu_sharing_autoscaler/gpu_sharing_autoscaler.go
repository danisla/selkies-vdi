/*
 Copyright 2019 Google Inc. All rights reserved.

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
*/

package main

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

func main() {
	// Set from downward API.
	namespace := os.Getenv("NAMESPACE")
	if len(namespace) == 0 {
		log.Fatal("Missing NAMESPACE env.")
	}

	gpusPerPod, err := strconv.Atoi(os.Getenv("NUM_GPU_RESOURCES_PER_POD"))
	if err != nil {
		log.Fatal("invalid value for env NUM_GPU_RESOURCES_PER_POD")
	}

	gpusPerNode, err := strconv.Atoi(os.Getenv("NUM_GPUS_PER_NODE"))
	if err != nil {
		log.Fatal("Failed to parse NUM_GPUS_PER_NODE env, expected integer.")
	}

	gpuPodSelector := os.Getenv("GPU_POD_SELECTOR")
	if len(gpuPodSelector) == 0 {
		log.Fatal("Missing env GPU_POD_SELECTOR")
	}

	replicaSelector := os.Getenv("REPLICASET_SELECTOR")
	if len(replicaSelector) == 0 {
		log.Fatal("Missing env REPLICASET_SELECTOR")
	}

	targetNodeUtilization, err := strconv.Atoi(os.Getenv("TARGET_GPU_NODE_UTILIZATION"))
	if err != nil {
		log.Fatal("Failed to parse TARGET_GPU_NODE_UTILIZATION, expected integer")
	}

	minIdleNodesEnv := os.Getenv("MIN_IDLE_NODES")
	if len(minIdleNodesEnv) == 0 {
		minIdleNodesEnv = "0"
	}
	minIdleNodes, err := strconv.Atoi(minIdleNodesEnv)
	if err != nil {
		log.Fatal("Failed to parse MIN_IDLE_NODES, expected integer")
	}

	log.Println("GPU sharing autoscaler controller started.")

	for {
		// Get pod status based on conditions.
		pods, err := getBrokerGPUPods(gpuPodSelector)
		if err != nil {
			log.Printf("failed to get pod status: %v", err)
		} else {
			// Get current replicaset count.
			rsName, numReplicas, err := getAutoscalerReplicaSetCount(namespace, replicaSelector)
			if err != nil {
				log.Printf("failed to get autoscaler replicas: %v", err)
			} else {
				// Compute number of replicas needed.
				targetNumReplicas := computeNumReplicas(gpusPerNode, gpusPerPod, targetNodeUtilization, numReplicas, len(pods), minIdleNodes)

				if numReplicas != targetNumReplicas {
					log.Printf("Requested GPUs: %d, current replicas: %d, scaling ReplicaSet %s to %d", len(pods)*gpusPerPod, numReplicas, rsName, targetNumReplicas)
					if err := scaleReplicaSet(namespace, rsName, targetNumReplicas); err != nil {
						log.Printf("Error scaling ReplicaSet: %v", err)
					}
				}
			}
		}

		time.Sleep(5 * time.Second)
	}

}

func computeNumReplicas(gpusPerNode, gpusPerPod, targetUtilization, numRSPods, numGPUPods, minIdleNodes int) int {
	targetPercentUtil := float64(targetUtilization) / float64(100)
	targetGPUsPerNode := math.Ceil(float64(gpusPerNode) * targetPercentUtil)
	maxPodsPerNode := math.Floor(targetGPUsPerNode / float64(gpusPerPod))
	targetNumReplicas := 0
	if int(maxPodsPerNode) == 0 {
		targetNumReplicas = numRSPods + minIdleNodes
	} else {
		targetNumReplicas = int(math.Ceil(float64(numGPUPods)/maxPodsPerNode)) + minIdleNodes
	}
	return targetNumReplicas
}

func getBrokerGPUPods(selector string) ([]string, error) {
	resp := make([]string, 0)

	cmd := exec.Command("sh", "-c", fmt.Sprintf("kubectl get pod --all-namespaces -l %s -o name 1>&2", selector))
	stdoutStderr, err := cmd.CombinedOutput()
	if err != nil {
		return resp, fmt.Errorf("failed to get pods: %s, %v", string(stdoutStderr), err)
	}

	resp = strings.Split(string(stdoutStderr), "\n")
	resp = resp[:len(resp)-1]

	return resp, nil
}

func getAutoscalerReplicaSetCount(namespace, selector string) (string, int, error) {
	rsName := ""
	rsCount := 0

	type rsSpec struct {
		Replicas int `json:"replicas"`
	}

	type rsObject struct {
		Metadata map[string]interface{} `json:"metadata"`
		Spec     rsSpec                 `json:"spec"`
	}

	type getRSSpec struct {
		Items []rsObject `json:"items"`
	}

	cmd := exec.Command("sh", "-c", fmt.Sprintf("kubectl get replicaset -n %s -l %s -o json 1>&2", namespace, selector))
	stdoutStderr, err := cmd.CombinedOutput()
	if err != nil {
		return rsName, rsCount, fmt.Errorf("failed to get replicaset: %s, %v", string(stdoutStderr), err)
	}

	var jsonResp getRSSpec
	if err := json.Unmarshal(stdoutStderr, &jsonResp); err != nil {
		return rsName, rsCount, fmt.Errorf("failed to parse replicaset spec: %v", err)
	}

	if len(jsonResp.Items) == 0 {
		return rsName, rsCount, fmt.Errorf("no replicaset found in namespace %s with selector: %s", namespace, selector)
	}

	rs := jsonResp.Items[0]

	rsName = rs.Metadata["name"].(string)
	rsCount = rs.Spec.Replicas

	return rsName, rsCount, nil
}

func scaleReplicaSet(namespace, rsName string, numReplicas int) error {

	cmd := exec.Command("sh", "-c", fmt.Sprintf("kubectl -n %s scale replicaset %s --replicas %d 1>&2", namespace, rsName, numReplicas))
	stdoutStderr, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("failed to get replicaset: %s, %v", string(stdoutStderr), err)
	}

	return nil
}
