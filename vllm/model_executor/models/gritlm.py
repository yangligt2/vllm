# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from array import array

import torch
import torch.nn as nn

from vllm.config import ModelConfig, VllmConfig
from vllm.logger import init_logger
from vllm.model_executor.layers.pooler import PoolerHead, PoolerNormalize
from vllm.model_executor.models.llama import LlamaForCausalLM
from vllm.model_executor.pooling_metadata import (PoolingMetadata,
                                                  PoolingTensors)
from vllm.sequence import PoolerOutput, PoolingSequenceGroupOutput
from vllm.transformers_utils.tokenizer import cached_tokenizer_from_config

from .interfaces import SupportsV0Only

logger = init_logger(__name__)


class GritLMPooler(nn.Module):

    def __init__(self, model_config: ModelConfig):
        super().__init__()

        self.model_config = model_config

        tokenizer = cached_tokenizer_from_config(self.model_config)

        # Collect the tokens needed for pattern matching.
        # "▁<" is different from "_<". The former uses "▁" to indicate that
        # the next token is the start of a word.
        # "<0x0A>" is the newline token (i.e. "\n")."
        self.token_ids = {
            tok: tokenizer.convert_tokens_to_ids([tok])[0]
            for tok in ["<s>", "▁<", "<", "|", "embed", ">", "<0x0A>", "user"]
        }

        def tokens_to_ids(tokens: list[str]) -> array:
            return array("i", [self.token_ids[token] for token in tokens])

        self.user_pattern_ids = tokens_to_ids(
            ["▁<", "|", "user", "|", ">", "<0x0A>"])
        self.embed_newline_pattern_ids = tokens_to_ids(
            ["<0x0A>", "<", "|", "embed", "|", ">", "<0x0A>"])
        self.embed_pattern_ids = tokens_to_ids(
            ["▁<", "|", "embed", "|", ">", "<0x0A>"])

        self.head = PoolerHead(PoolerNormalize())

    def _find_array(self, arr: array, target: array, start_idx: int) -> int:
        """
        Find the first occurrence of target in arr starting from start_idx.

        Args:
        arr: The array to search within
        target: The consecutive subsequence to find
        start_idx: The starting index to search from

        Returns:
        int: The index of the first occurrence of target in arr.
        """
        if start_idx < 0:
            raise ValueError("start_idx must be non-negative")
        if not target or not arr:
            raise ValueError("Empty arr or target not allowed")

        target_len = len(target)
        for i in range(start_idx, len(arr) - target_len + 1):
            if arr[i:i + target_len] == target:
                return i
        return -1

    def _get_instruction_len(self, prompt_token_ids: array) -> int:
        """
        Get the length of the instruction in the prompt.

        We do a pattern matching to find the instruction in the prompt,
        and then return the length of the instruction.

        The pattern matching is done using integers instead of strings
        because the prompt is given as a list of token IDs.
        """

        instruction_len = 0

        # Return no instruction in case of missing BOS token.
        if prompt_token_ids[0] != self.token_ids["<s>"]:
            logger.warning("BOS token not found in prompt, "
                           "thus using empty string for instruction. "
                           "GritLM requires BOS token in prompt.")
            return instruction_len

        # If user pattern is found in the prompt, that means there should be
        # a newline token before the embed pattern.
        embed_pattern_ids = self.embed_pattern_ids
        if self._find_array(prompt_token_ids,
                            self.user_pattern_ids,
                            start_idx=1) == 1:
            embed_pattern_ids = self.embed_newline_pattern_ids

        # Find the embed pattern in the prompt.
        found_embed_pattern_idx = self._find_array(prompt_token_ids,
                                                   embed_pattern_ids,
                                                   start_idx=1)

        if found_embed_pattern_idx != -1:
            instruction_len = found_embed_pattern_idx + len(embed_pattern_ids)
        else:
            logger.warning("Query instruction not found in prompt, "
                           "thus using BOS token as instruction instead. "
                           "GritLM requires query instruction in prompt.")
            instruction_len = 1

        return instruction_len

    def forward(
        self,
        hidden_states: torch.Tensor,
        pooling_metadata: PoolingMetadata,
    ) -> PoolerOutput:
        """
        Pool the hidden states by summing the embeddings of
        non-instruction tokens.
        """
        prompts_token_ids = [
            token_ids.prompt_token_ids_array
            for _, token_ids in pooling_metadata.seq_data.items()
        ]

        instruction_lens = torch.tensor(
            [
                self._get_instruction_len(prompt_token_ids)
                for prompt_token_ids in prompts_token_ids
            ],
            device=hidden_states.device,
        )

        prompt_lens = PoolingTensors.from_pooling_metadata(
            pooling_metadata, hidden_states.device).prompt_lens

        mask = torch.zeros_like(hidden_states, dtype=torch.bool)

        start_idx = 0
        for prompt_len, instruction_len in zip(prompt_lens, instruction_lens):
            end_idx = start_idx + prompt_len
            mask[start_idx + instruction_len:end_idx] = True
            start_idx = end_idx

        masked_hidden_states = hidden_states.masked_fill(~mask, 0.0)

        sum_embeddings = torch.zeros(len(prompt_lens),
                                     hidden_states.size(1),
                                     device=hidden_states.device)

        start_idx = 0
        for i, prompt_len in enumerate(prompt_lens):
            end_idx = start_idx + prompt_len
            sum_embeddings[i] = masked_hidden_states[start_idx:end_idx].sum(
                dim=0)
            start_idx = end_idx

        num_non_instruction_tokens = prompt_lens - instruction_lens
        mean_embeddings = sum_embeddings / num_non_instruction_tokens.unsqueeze(
            1)

        pooled_data = self.head(mean_embeddings,
                                pooling_metadata=pooling_metadata)

        pooled_outputs = [
            PoolingSequenceGroupOutput(data) for data in pooled_data
        ]

        return PoolerOutput(outputs=pooled_outputs)


class GritLM(LlamaForCausalLM, SupportsV0Only):
    """This class implements the embedding model for parasail-ai/GritLM-7B-vllm.

    The class inherits from LlamaForCausalLM and provides a custom pooling
    layer.

    The main difference between the pooling layer in GritLM and the one in
    LlamaForCausalLM is that GritLM ignores the query instruction in the prompt
    when pooling the hidden states.

    Embedding prompts should be in the following format:
    - With instruction: "<|user|>\nINSTRUCTION\n<|embed|>\nPROMPT".
    - Without instruction: "<|embed|>\nPROMPT".

    Generation prompts should be in the following format:
    - "<|user|>\nPROMPT\n<|assistant|>\n"
    """

    is_pooling_model = True

    def __init__(
        self,
        vllm_config: VllmConfig,
        prefix: str = "",
        **kwargs,
    ) -> None:
        # Use full attention for pooling
        if vllm_config.model_config.runner_type == "pooling":
            hf_config = vllm_config.model_config.hf_config
            hf_config.is_causal = False

            vllm_config.cache_config.sliding_window = None

            for attr in ("sliding_window", "interleaved_sliding_window"):
                if hasattr(hf_config, attr):
                    delattr(hf_config, attr)

        super().__init__(vllm_config=vllm_config, prefix=prefix, **kwargs)

        self.pooler = GritLMPooler(vllm_config.model_config)
