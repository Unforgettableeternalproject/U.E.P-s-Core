import json
import random
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Tuple

class NLPTrainingDataGenerator:
    def __init__(self):
        # Define various templates and patterns for each category
        
        # CALL patterns - greetings and attention getters
        self.call_patterns = [
            "Hello UEP",
            "Hey UEP",
            "Hi there UEP",
            "UEP, are you there?",
            "Hey assistant",
            "Hello assistant",
            "Hi",
            "Hello",
            "Hey",
            "Are you there?",
            "Can you hear me?",
            "Wake up UEP",
            "UEP wake up",
            "Hey UEP, wake up",
            "Excuse me UEP",
            "UEP?",
            "Hello?",
            "Anyone there?",
            "UEP, you there?",
            "Hey UEP, are you listening?",
            "Good morning UEP",
            "Good afternoon UEP",
            "Good evening UEP",
            "Greetings UEP",
            "Hi UEP, are you available?",
            "UEP, can you help me?",
            "Hey there",
            "Yo UEP",
            "UEP, I need you",
            "Attention UEP",
            "UEP please respond",
            "UEP, are you online?",
            "Is anyone there?",
            "UEP, you awake?",
            "Testing, UEP?",
            "UEP, can you hear me?",
            "Hey buddy",
            "Hi friend",
            "Hello there UEP",
            "UEP, are you ready?",
            "Wake up assistant",
            "Hey system",
            "Hello system",
            "System, are you there?",
            "UEP, status?",
            "UEP, report",
            "Hey UEP, you up?",
            "Morning UEP",
            "Evening UEP",
            "UEP, hello"
        ]
        
        # CHAT patterns - casual conversation
        self.chat_patterns = [
            "The weather is beautiful today",
            "I had a great day",
            "I'm feeling happy",
            "Just thinking about life",
            "I watched a great movie",
            "The coffee tastes amazing",
            "I love this music",
            "Today was interesting",
            "I met someone new today",
            "The sunset looks beautiful",
            "I'm excited about tomorrow",
            "Life is good",
            "I'm learning new things",
            "This book is fascinating",
            "I enjoyed lunch today",
            "My family is doing well",
            "I'm grateful for today",
            "The park was peaceful",
            "I saw something funny",
            "Work was productive",
            "I'm feeling creative",
            "The food was delicious",
            "I had a nice walk",
            "My friend called me",
            "I'm thinking about vacation",
            "The game was exciting",
            "I learned something new",
            "My pet is adorable",
            "I'm feeling relaxed",
            "The city looks nice",
            "I had a good workout",
            "The museum was interesting",
            "I'm enjoying this moment",
            "My plants are growing",
            "I saw a rainbow",
            "The concert was amazing",
            "I'm feeling inspired",
            "Today's news was interesting",
            "I made progress today",
            "The air feels fresh",
            "I'm thinking about the future",
            "My project is going well",
            "I discovered something cool",
            "The beach was relaxing",
            "I'm feeling peaceful",
            "The stars look bright",
            "I had a nice dream",
            "My hobby is fun",
            "I'm enjoying the silence",
            "The journey was worth it",
            "I feel accomplished",
            "Just wanted to share that",
            "Isn't that interesting?",
            "I wonder about things sometimes",
            "Life has been treating me well",
            "I've been thinking lately",
            "You know what's funny?",
            "I just realized something",
            "That reminds me of something",
            "I've been meaning to tell you",
            "Guess what happened today",
            "You won't believe what I saw",
            "I had the strangest dream",
            "Something cool happened",
            "I've been pondering",
            "Just a random thought",
            "I find it fascinating that",
            "It's interesting how",
            "I've always wondered",
            "Did you know that",
            "I just learned that"
        ]
        
        # COMMAND patterns - action requests
        self.command_patterns = [
            "Set a reminder for tomorrow",
            "Open my calendar",
            "Show me my schedule",
            "Create a new note",
            "Search for tutorials",
            "Turn on the lights",
            "Play some music",
            "Set an alarm for 7 AM",
            "Send a message",
            "Check my email",
            "Show me the weather",
            "Calculate this for me",
            "Find restaurants nearby",
            "Book a meeting",
            "Cancel my appointment",
            "Add this to my list",
            "Delete that file",
            "Update my profile",
            "Download the document",
            "Save this information",
            "Close all windows",
            "Restart the system",
            "Check system status",
            "Run diagnostics",
            "Generate a report",
            "Analyze this data",
            "Translate this text",
            "Convert this file",
            "Backup my data",
            "Restore settings",
            "Clear the cache",
            "Install updates",
            "Configure settings",
            "Export the results",
            "Import the file",
            "Sync my devices",
            "Share this link",
            "Copy this text",
            "Paste the content",
            "Undo last action",
            "Help me with my schedule",
            "Organize my tasks",
            "Sort by date",
            "Filter the results",
            "Search in documents",
            "Find my files",
            "Create a backup",
            "Test the connection",
            "Monitor the process",
            "Track my progress",
            "Set a timer for 10 minutes",
            "Wake me up at 6 AM",
            "Remind me to call John",
            "Schedule a meeting for Monday",
            "Book a flight to New York",
            "Order pizza for dinner",
            "Call my mom",
            "Text my friend",
            "Email my boss",
            "Print this document",
            "Scan this page",
            "Take a screenshot",
            "Record audio",
            "Start video recording",
            "Stop the timer",
            "Pause the music",
            "Skip this song",
            "Increase volume",
            "Decrease brightness",
            "Switch to dark mode",
            "Enable notifications",
            "Disable auto-update",
            "Block this contact",
            "Add to favorites",
            "Remove from list",
            "Mark as complete",
            "Flag for review",
            "Archive this conversation",
            "Pin this note",
            "Lock the screen",
            "Sign me out",
            "Change my password",
            "Update my status",
            "Edit my profile",
            "Delete my account",
            "Clear history",
            "Empty trash",
            "Compress this folder",
            "Extract files",
            "Merge documents",
            "Split the PDF",
            "Rotate image",
            "Crop the photo",
            "Adjust colors",
            "Apply filter",
            "Save as template",
            "Load preset",
            "Reset to default",
            "Optimize performance"
        ]
        
        # Variations and modifiers
        self.politeness_modifiers = ["please", "could you", "would you mind", "can you", "I'd like you to", "kindly"]
        self.time_modifiers = ["now", "immediately", "quickly", "when you can", "as soon as possible", "later", "tomorrow", "next week"]
        self.transition_words = ["anyway", "by the way", "also", "oh and", "additionally", "furthermore", "besides", "speaking of which"]
        
    def tokenize(self, text: str) -> List[str]:
        """Simple tokenization by splitting on spaces and punctuation"""
        import re
        # Split on whitespace and keep punctuation as separate tokens
        tokens = re.findall(r'\w+|[^\w\s]', text)
        return tokens
    
    def generate_bio_labels(self, text: str, segments: List[Dict]) -> Tuple[List[str], List[str]]:
        """Generate BIO labels for tokenized text based on segments"""
        tokens = self.tokenize(text)
        bio_labels = ['O'] * len(tokens)
        
        char_to_token = {}
        char_pos = 0
        for i, token in enumerate(tokens):
            # Find token position in original text
            while char_pos < len(text) and text[char_pos:char_pos+len(token)] != token:
                char_pos += 1
            for j in range(len(token)):
                char_to_token[char_pos + j] = i
            char_pos += len(token)
        
        for segment in segments:
            label = segment['label']
            start = segment['start']
            end = segment['end']
            
            # Find tokens in this segment
            segment_tokens = set()
            for char_idx in range(start, min(end, len(text))):
                if char_idx in char_to_token:
                    segment_tokens.add(char_to_token[char_idx])
            
            # Apply BIO labels
            segment_tokens = sorted(segment_tokens)
            for i, token_idx in enumerate(segment_tokens):
                if i == 0:
                    bio_labels[token_idx] = f'B-{label}'
                else:
                    bio_labels[token_idx] = f'I-{label}'
        
        return tokens, bio_labels
    
    def create_simple_example(self, pattern: str, label: str) -> Dict:
        """Create a simple single-label example"""
        text = pattern
        tokens, bio_labels = self.generate_bio_labels(
            text, 
            [{"text": text, "label": label, "start": 0, "end": len(text), "confidence": 1.0, "annotator_notes": ""}]
        )
        
        return {
            "text": text,
            "tokens": tokens,
            "bio_labels": bio_labels,
            "segments": [{"text": text, "label": label, "start": 0, "end": len(text), "confidence": 1.0, "annotator_notes": ""}]
        }
    
    def create_compound_example(self) -> Dict:
        """Create a compound example with multiple segments"""
        segments = []
        parts = []
        current_pos = 0
        
        # Randomly decide composition (2-3 segments)
        num_segments = random.randint(2, 3)
        segment_types = random.sample(['CALL', 'CHAT', 'COMMAND'], num_segments)
        
        for i, seg_type in enumerate(segment_types):
            if seg_type == 'CALL':
                text = random.choice(self.call_patterns)
            elif seg_type == 'CHAT':
                text = random.choice(self.chat_patterns)
            else:  # COMMAND
                text = random.choice(self.command_patterns)
                # Sometimes add politeness
                if random.random() < 0.3:
                    modifier = random.choice(self.politeness_modifiers)
                    text = f"{modifier} {text.lower()}"
            
            # Add transition word between segments (except for first)
            if i > 0 and random.random() < 0.5:
                transition = random.choice(self.transition_words).capitalize()
                parts.append(f"{transition},")
                current_pos = sum(len(p) + 1 for p in parts[:-1])  # +1 for space
            
            # Add segment
            start = current_pos if i == 0 else current_pos + len(parts[-1]) + 1 if parts else current_pos
            parts.append(text)
            end = start + len(text)
            
            segments.append({
                "text": text,
                "label": seg_type,
                "start": start,
                "end": end,
                "confidence": 1.0,
                "annotator_notes": ""
            })
            
            current_pos = end
            
            # Add punctuation between segments
            if i < num_segments - 1:
                if random.random() < 0.5:
                    parts.append(".")
                else:
                    parts.append(",")
                current_pos += 2  # punctuation + space
        
        # Combine parts
        full_text = " ".join(parts)
        
        # Adjust segment positions for the final text
        adjusted_segments = []
        for segment in segments:
            seg_text = segment["text"]
            seg_start = full_text.find(seg_text)
            if seg_start != -1:
                adjusted_segments.append({
                    "text": seg_text,
                    "label": segment["label"],
                    "start": seg_start,
                    "end": seg_start + len(seg_text),
                    "confidence": 1.0,
                    "annotator_notes": ""
                })
        
        tokens, bio_labels = self.generate_bio_labels(full_text, adjusted_segments)
        
        return {
            "text": full_text,
            "tokens": tokens,
            "bio_labels": bio_labels,
            "segments": adjusted_segments
        }
    
    def generate_dataset(self, num_samples: int = 2500) -> List[Dict]:
        """Generate complete dataset with metadata"""
        dataset = []
        used_texts = set()  # To avoid duplicates
        
        # Ensure good distribution
        min_per_category = num_samples // 6  # At least 1/6 for each simple type
        min_compound = num_samples // 3  # At least 1/3 compound
        
        # Generate simple CALL examples
        call_count = 0
        for pattern in self.call_patterns:
            if call_count >= min_per_category:
                break
            example = self.create_simple_example(pattern, "CALL")
            if example["text"] not in used_texts:
                example_id = hashlib.md5(f"call_{call_count}".encode()).hexdigest()[:8]
                dataset.append({
                    "id": f"call_{example_id}",
                    **example,
                    "metadata": {
                        "source": "generated_dataset",
                        "scenario": "simple_call",
                        "created_date": datetime.now().isoformat(),
                        "annotated": True,
                        "quality_checked": False,
                        "annotator": "auto_generator",
                        "annotation_date": datetime.now().isoformat()
                    }
                })
                used_texts.add(example["text"])
                call_count += 1
        
        # Generate simple CHAT examples
        chat_count = 0
        for pattern in self.chat_patterns:
            if chat_count >= min_per_category:
                break
            example = self.create_simple_example(pattern, "CHAT")
            if example["text"] not in used_texts:
                example_id = hashlib.md5(f"chat_{chat_count}".encode()).hexdigest()[:8]
                dataset.append({
                    "id": f"chat_{example_id}",
                    **example,
                    "metadata": {
                        "source": "generated_dataset",
                        "scenario": "simple_chat",
                        "created_date": datetime.now().isoformat(),
                        "annotated": True,
                        "quality_checked": False,
                        "annotator": "auto_generator",
                        "annotation_date": datetime.now().isoformat()
                    }
                })
                used_texts.add(example["text"])
                chat_count += 1
        
        # Generate simple COMMAND examples
        command_count = 0
        for pattern in self.command_patterns:
            if command_count >= min_per_category:
                break
            example = self.create_simple_example(pattern, "COMMAND")
            if example["text"] not in used_texts:
                example_id = hashlib.md5(f"command_{command_count}".encode()).hexdigest()[:8]
                dataset.append({
                    "id": f"command_{example_id}",
                    **example,
                    "metadata": {
                        "source": "generated_dataset",
                        "scenario": "simple_command",
                        "created_date": datetime.now().isoformat(),
                        "annotated": True,
                        "quality_checked": False,
                        "annotator": "auto_generator",
                        "annotation_date": datetime.now().isoformat()
                    }
                })
                used_texts.add(example["text"])
                command_count += 1
        
        # Generate compound examples
        compound_count = 0
        attempts = 0
        max_attempts = min_compound * 3  # Prevent infinite loop
        
        while compound_count < min_compound and attempts < max_attempts:
            attempts += 1
            example = self.create_compound_example()
            if example["text"] not in used_texts:
                example_id = hashlib.md5(f"compound_{compound_count}".encode()).hexdigest()[:8]
                dataset.append({
                    "id": f"compound_{example_id}",
                    **example,
                    "metadata": {
                        "source": "generated_dataset",
                        "scenario": "compound_interaction",
                        "created_date": datetime.now().isoformat(),
                        "annotated": True,
                        "quality_checked": False,
                        "annotator": "auto_generator",
                        "annotation_date": datetime.now().isoformat()
                    }
                })
                used_texts.add(example["text"])
                compound_count += 1
        
        # Fill remaining with varied examples
        while len(dataset) < num_samples:
            if random.random() < 0.4:  # 40% compound
                example = self.create_compound_example()
                scenario = "compound_interaction"
            else:
                # Random simple example
                category = random.choice(['CALL', 'CHAT', 'COMMAND'])
                if category == 'CALL':
                    pattern = random.choice(self.call_patterns)
                    scenario = "simple_call"
                elif category == 'CHAT':
                    pattern = random.choice(self.chat_patterns)
                    scenario = "simple_chat"
                else:
                    pattern = random.choice(self.command_patterns)
                    scenario = "simple_command"
                    
                example = self.create_simple_example(pattern, category)
            
            if example["text"] not in used_texts:
                example_id = hashlib.md5(f"mixed_{len(dataset)}".encode()).hexdigest()[:8]
                dataset.append({
                    "id": f"{scenario}_{example_id}",
                    **example,
                    "metadata": {
                        "source": "generated_dataset",
                        "scenario": scenario,
                        "created_date": datetime.now().isoformat(),
                        "annotated": True,
                        "quality_checked": False,
                        "annotator": "auto_generator",
                        "annotation_date": datetime.now().isoformat()
                    }
                })
                used_texts.add(example["text"])
        
        # Shuffle for better training
        random.shuffle(dataset)
        
        return dataset

# Generate the dataset
generator = NLPTrainingDataGenerator()
dataset = generator.generate_dataset(2500)

# Save to JSONL format (one JSON object per line)
output_lines = []
for item in dataset:
    output_lines.append(json.dumps(item))

# Print first 10 examples as preview
print("First 10 examples from the generated dataset:")
print("=" * 80)
for i in range(min(10, len(dataset))):
    data = dataset[i]
    print(f"\nExample {i+1}:")
    print(f"ID: {data['id']}")
    print(f"Text: {data['text']}")
    print(f"Tokens: {data['tokens']}")
    print(f"BIO Labels: {data['bio_labels']}")
    print(f"Segments: {data['segments']}")

print(f"\n{'=' * 80}")
print(f"Total samples generated: {len(dataset)}")

# Statistics
call_count = sum(1 for d in dataset if 'CALL' in [s['label'] for s in d['segments']])
chat_count = sum(1 for d in dataset if 'CHAT' in [s['label'] for s in d['segments']])
command_count = sum(1 for d in dataset if 'COMMAND' in [s['label'] for s in d['segments']])
compound_count = sum(1 for d in dataset if len(d['segments']) > 1)

print(f"\nDataset Statistics:")
print(f"- Samples with CALL: {call_count}")
print(f"- Samples with CHAT: {chat_count}")
print(f"- Samples with COMMAND: {command_count}")
print(f"- Compound samples: {compound_count}")
print(f"- Simple samples: {len(dataset) - compound_count}")

# Save to file
output_filename = "nlp_training_data.jsonl"
with open(output_filename, 'w') as f:
    for line in output_lines:
        f.write(line + '\n')

print(f"\nDataset saved to: {output_filename}")
print("Format: JSONL (one JSON object per line)")