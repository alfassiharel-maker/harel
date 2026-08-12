<!--
Extracted from the Master Specification PDF supplied with the project
(*Logic Management, Reasoning & Code Intelligence Language*, v1.0).

This is a machine text extraction. The original mixes Hebrew and English, and
extraction does not always preserve right-to-left ordering inside a mixed line,
so some sentences read out of order. The PDF is the authority; this copy is here
so that the specification travels with the repository and so that a reference
like "Master Spec §56" can be checked without it.

Nothing in this file is edited: it is the specification as issued, not as
interpreted. The interpretation lives in 01–05 and in OPEN_DESIGN_DECISIONS.md.
-->

# MASTER SPECIFICATION (as supplied)

MASTER SPECIFICATION
Logic Management, Reasoning & Code Intelligence Language
Status: Foundational Specification
Version: 1.0
Purpose: Source of truth for design and implementation
Target coding agent: Claude Opus-class / Claude Code
Project state: Build from zero; no existing implementation may be assumed
1. EXECUTIVE DEFINITION
הפרויקט הוא בניית שפת תכנות ומערכת ביצוע חדשה ,חוקים, שמטרתה להפוך לוגיקהreasoning, state, נתונים
ופעולת קוד לאובייקטים מפורשים שניתן:
להגדיר
להריץ
לנתח
לשנות
לבדוק
להסביר
לחקור
לבצע אופטימיזציה
ולשלב עם נתונים חיצוניים
עוד שפת תכנות כללית "המערכת אינה מיועדת להיות."
היא מיועדת להיות שכבת תכנות חדשה שבה:
CODE
LOGIC
RULES
FACTS
STATE
DATA
REASONING
EXECUTION
הם חלקים רשמיים של אותו מודל.
המטרה ארוכת הטווח היא ליצור מערכת שבה ניתן לא רק להריץ קוד ,אלא גם לנתח את המבנה הלוגי של הקוד
עצמו ,לייצג את הקשרים בין רכיביו ולחקור כיצד קלט הופך להחלטה או לפלט.
 •
 •
 •
 •
 •
 •
 •
 •
 •
1
2. TARGET USERS
2.1 Professional Developers
מפתחים שבונים מערכות מורכבות מבוססות:
logic
business rules
data
decision systems
automation
reasoning
2.2 Logic Researchers
חוקרים הבוחנים:
formal logic
inference
rule systems
symbolic reasoning
decision processes
computational reasoning
2.3 AI Researchers
לבדוק ולנתח, חוקרים ומפתחים המעוניינים לייצגreasoning בצורה מפורשת.
2.4 Enterprise Data Organizations
מערכות חוקים, ארגונים גדולים עם כמויות נתונים משמעותיותworkflows ומנועי החלטה.
3. CORE VISION
המערכת צריכה לאפשר תהליך כללי מהצורה:
INPUT
  ↓
INTERPRETATION
  ↓
FACTS
  ↓
STATE
  ↓
RULES
  ↓
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
2
INFERENCE
  ↓
DERIVED FACTS
  ↓
ADDITIONAL REASONING
  ↓
DECISION
  ↓
OUTPUT
אבל חשוב:
התרשים הוא מודל קונספטואלי ,לאsyntax.
ה־semantics המדויקים יוגדרו בהמשך במסמך השפה.
4. THE CENTRAL IDEA
המערכת בנויה סביב רעיון מרכזי:
Logic should be an executable, inspectable and transformable representation.
כלומר:
לוגיקה אינה רק משהו שהמתכנת כותב.
היא גם:
מידע שניתן לנתח
מבנה שניתן להציג
אובייקט שניתן לשנות
תהליך שניתן לעקוב אחריו
מערכת שניתן לבדוק
גרף שלdependencies
מקור להפקת מסקנות
5. CODE-AS-DATA
אחד מעקרונות היסוד של השפה הוא שהקוד עצמו אינוopaque בלבד.
המערכת צריכה לייצג תוכנית בצורה מבנית:
SOURCE
  ↓
• 
• 
• 
• 
• 
• 
• 
3
TOKENS
  ↓
AST
  ↓
SEMANTIC MODEL
  ↓
IR
  ↓
EXECUTION PLAN
ה־AST וה־IR אינם רקartifacts פנימיים שלcompiler .
בהמשך הם צריכים להיות נגישים ל־language tooling וליכולותmeta-programming מוגדרות.
המערכת צריכה לאפשר בעתיד לבצע פעולות כמו:
inspect(code)
analyze(code)
transform(code)
generate(code)
validate(code)
explain(code)
אין להניח שהפעולות הללו קיימות ב־MVP .
יש לתכנן את הארכיטקטורה כך שניתן יהיה להוסיף אותן ללא שבירת ה־language semantics.
6. LOGIC AS A FIRST-CLASS CONCEPT
Logic היאprimitive של השפה.
היא אינהabstraction שמחקה באמצעותclasses אוfunctions.
המערכת צריכה לכלולconcepts מפורשים עבור:
FACT
RULE
CONDITION
INFERENCE
STATE
QUERY
DECISION
TRACE
4
7. FACT MODEL
Fact הוא מידע שהמערכת יכולה להשתמש בו בתהליךreasoning.
דוגמה קונספטואלית:
fact temperature = 31
fact user.age = 19
fact account.status = "active"
Facts יכולים להגיע מ:
source code
runtime input
SQL query
external data source
derived rule
state transition
אין להניח שכלFact הואmutable.
ה־semantic model חייב להגדיר זאת במפורש.
8. RULE MODEL
Rule מגדיר קשר בין תנאים לבין תוצאה.
מודל מופשט:
WHEN condition
THEN consequence
לדוגמה קונספטואלית:
rule high_temperature:
    when temperature > 30
    then heat_state = high
ה־syntax בדוגמה אינו סופי.
ה־semantics חייבים להגדיר:
מתיrule נחשבactive
כיצדrule מופעל
1. 
2. 
3. 
4. 
5. 
6. 
• 
• 
5
האםrule יכול לפעול יותר מפעם אחת
מה קורה כאשרstate משתנה
מה קורה כאשרrules מתנגשים
מה קורה בעתcycle
מהי קדימותrule
האםrules טהורים או יכולים לגרוםside effects
אין להמציאanswers.
9. REASONING ENGINE
Reasoning הוא תהליך הפקת מידע חדש מתוך:
FACTS
+
RULES
+
STATE
+
DATA
לדוגמה:
Fact A
   ↓
Rule 1
   ↓
Fact B
   ↓
Rule 2
   ↓
Fact C
   ↓
Decision
המנוע חייב לשמור מספיק מידע כדי לאפשרreconstruction של מסלולreasoning בהתאם למדיניות ה־trace.
10. REASONING TRACE
Execution צריך להיות ניתן לחקירה.
Trace model ראשוני:
 •
 •
 •
 •
 •
 •
6
Execution ID
Input
Initial State
Loaded Facts
Activated Rules
Conditions Evaluated
Derived Facts
State Changes
SQL Queries
Intermediate Results
Final Result
Errors
Timing
המטרה:
לאפשר לשאול:
Why did this result happen?
והמערכת תוכל לייצרexplanation מתוךexecution trace ולא מתוך ניחוש.
11. STATE MODEL
המערכת חייבת להבחין בין:
INPUT
FACT
DERIVED FACT
STATE
OUTPUT
למשל:
Input:
temperature = 31
Derived:
heat_state = high
Decision:
cooling = enabled
7
Output:
system_action = activate_cooling
ה־state model חייב להיותformalized לפניimplementation.
12. SQL AS A FIRST-CLASS DATA INTERFACE
SQL הוא חלק מרכזי מהמערכת.
המטרה היא לאפשר:
SQL
 ↓
DATA
 ↓
FACTS
 ↓
LOGIC
 ↓
REASONING
וגם:
FACTS
 ↓
LOGIC
 ↓
QUERY
 ↓
SQL
 ↓
RESULT
השפה לא צריכה להמציא מחדשdatabase language.
במקום זאת היא צריכה לספקabstraction בטוח בין ה־logic engine לביןdatabase systems.
13. SQL SECURITY
המערכת חייבת להפריד בין:
8
READ
WRITE
ADMIN
ולספקpermission model.
יש לתכנן לפחות:
Query Validation
Connection Isolation
Credential Isolation
Resource Limits
Permission Checks
Auditability
אין לאפשרarbitrary database access מתוךlogic runtime ללאauthorization.
14. LANGUAGE INFLUENCES
השפה אינהfork של שפה קיימת.
היא צריכה ללמוד עקרונות מכמה משפחות שונות.
Rust
למימוש:
memory safety
systems programming
concurrency
performance
compiler/runtime engineering
reliable production core
Prolog
להשראה עבור:
facts
rules
unification
inference
backtracking
declarative reasoning
אין להעתיק אתsyntax שלProlog.
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
9
Datalog
להשראה עבור:
relational logic
rule evaluation
data-driven inference
connection ביןlogic ל־datasets
Lisp / Scheme
להשראה עבור:
code-as-data
metaprogramming
macros
structural representation
transformation של תוכניות
Haskell
להשראה עבור:
type systems
algebraic data types
purity
semantics
functional abstraction
formal reasoning
SQL
למודל הנתונים והשאילתות.
Python
להשראה עבור:
accessibility
experimentation
rapid tooling
scripting
integration
Python אינו בהכרח חלק מה־runtime core.
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
• 
10
15. LANGUAGE DESIGN PRINCIPLE
השפה החדשה צריכה לשאוף לשילוב:
Declarative Logic
+
Imperative Control
+
Data Querying
+
Meta-Programming
+
Strong Semantics
+
Production Systems Engineering
אין להניח מראש שכל אחד מהתחומים הללו יופיע באותה שכבתsyntax.
16. SYNTAX POLICY
אין להעתיק:
Prolog syntax
Lisp syntax
Rust syntax
Python syntax
SQL syntax
באופן מלא.
השפות הקיימות הןsources of design insight בלבד.
יש ליצורsyntax חדש שמטרתו:
readability
precise semantics
inspectability
composability
machine analysis
future metaprogramming
17. TYPE SYSTEM
המערכת צריכה להיותtyped.
• 
• 
• 
• 
• 
1. 
2. 
3. 
4. 
5. 
6. 
11
Candidate primitive types:
Integer
Float
Boolean
String
Null
List
Map
Record
Candidate future types:
Set
Tuple
Enum
Option
Result
Custom Type
Logical Value
Fact
Rule
Query
State
הרשימה אינהnal.
לפניimplementation יש להחליט אילוentities הם באמתlanguage-level types.
18. CODE REPRESENTATION
כל תוכנית צריכה לעבורpipeline:
Source
 ↓
Lexer
 ↓
Parser
 ↓
AST
 ↓
Semantic Analysis
 ↓
Typed/Validated AST
 ↓
12
IR
 ↓
Execution Plan
 ↓
Runtime
19. AST REQUIREMENTS
AST צריך לייצג לפחות:
Program
Declaration
Expression
Fact
Rule
Condition
Query
State Operation
Output
בעתיד:
Macro
Code Object
Logic Object
Meta Transformation
20. IR
ה־Intermediate Representation הואcontract בין השפה לביןruntime.
הוא צריך להיות:
language-independent ככל האפשר
explicit
inspectable
versionable
testable
ה־IR צריך להפריד ביןsource syntax לביןexecution implementation.
• 
• 
• 
• 
• 
13
21. EXECUTION MODEL
Execution engine אחראי על:
Load Program
Initialize Environment
Load Facts
Evaluate Conditions
Activate Rules
Derive Facts
Update State
Execute Queries
Generate Output
Record Trace
יש להגדיר במפורש:
evaluation order
rule scheduling
conflict resolution
termination
recursion
cycles
side effects
determinism
לפניimplementation.
22. DETERMINISM
ברירת המחדל צריכה להיות:
אותם:
Source
Input
Data
State
Runtime Version
Configuration
מייצרים אותה תוצאה.
אם המערכת מאפשרתnondeterminism, יש להגדיר זאת במפורש ולתעד את מקורו.
 •
 •
 •
 •
 •
 •
 •
 •
14
23. CONFLICTS
כאשר מספרrules מביאות לתוצאות שונות:
Rule A → X
Rule B → Y
המערכת חייבת להשתמש ב־defined conflict semantics.
אפשרויות אפשריות:
priority
specificity
explicit ordering
conflict error
multi-result
custom strategy
אבל אין לבחור אחת לפניDesign Review.
24. CYCLES
המערכת חייבת לזהות או לשלוט ב:
A → B
B → C
C → A
אין להסתמך עלinfinite runtime behavior .
יש להגדירpolicy כגון:
error
iteration limit
fixed point
explicit recursion
לא לפניspecification.
15
25. ERROR SYSTEM
סוגי שגיאות:
Lexical Error
Syntax Error
Type Error
Semantic Error
Logic Error
Runtime Error
SQL Error
Data Error
Security Error
Resource Error
כלerror צריך לכלול ככל האפשר:
Error Code
Location
Message
Context
Cause
Suggested Resolution
26. CORE ARCHITECTURE
המערכת תהיה מודולרית:
┌─────────────────────────────┐
│            IDE              │
├─────────────────────────────┤
│       Language Tooling      │
├─────────────────────────────┤
│ Lexer / Parser / AST        │
├─────────────────────────────┤
│ Semantic Engine             │
├─────────────────────────────┤
│ IR / Planner                │
├─────────────────────────────┤
│ Logic Engine                │
├─────────────────────────────┤
│ Reasoning Engine            │
├─────────────────────────────┤
│ Runtime                     │
16
├─────────────────────────────┤
│ SQL / Data Layer            │
├─────────────────────────────┤
│ Storage / External Systems  │
└─────────────────────────────┘
27. RECOMMENDED TECHNOLOGY STACK
Core language
Rust
ה־core production implementation צריך להיותRust unless a later architecture review proves a concrete
reason אחרת.
Parser
Rust-based parser technology.
יש לבצע בחירה ביןparser combinators לביןparser generator לאחר הגדרתgrammar .
Database
Abstract SQL layer .
Initial targets can include:
SQLite
PostgreSQL
DuckDB
אך אין להכניס שלושתם ל־MVP ללא צורך.
Desktop application
אפשרות ראשונית:
Tauri
+
TypeScript
+
Rust Core
17
CLI
Rust CLI.
Testing
Rust unit/integration tests + end-to-end tests.
28. WHY RUST IS THE CORE
Rust נבחרת לא בגלל התחביר שלה אלא בגללproperties הנדרשים ל־production engine:
Memory Safety
Native Performance
Concurrency
Deterministic Resource Management
FFI Capability
Cross-Platform Deployment
Compiler Ecosystem
29. PYTHON ROLE
Python יכול לשמש עבור:
Research
Experiments
Data Analysis
Prototyping
AI Integrations
Developer Utilities
Benchmark Analysis
אבלPython אינוsource of truth עבורsemantics של ה־language.
ה־core behavior חייב להיות מוגדר וממומש ב־production runtime.
30. CLI
המערכת חייבת לספקCLI עצמאי.
דוגמה קונספטואלית:
18
language check
language build
language run
language test
language trace
language query
language inspect
language format
שמות הפקודות הסופיים ייקבעו בנפרד.
31. IDE
ה־IDE הוא שכבתproduct, לא ה־language core.
Capabilities:
Editor
Syntax Highlighting
Autocomplete
Diagnostics
Project Explorer
Execution Console
Debugger
Trace Viewer
Logic Graph
SQL Explorer
אין לבנות את ה־IDE לפני שה־language core וה־CLI יציבים מספיק.
32. DEBUGGER
הdebugger צריך להביןentities של השפה.
לא רקlines.
צריך להיות אפשרי לחקור:
Current Fact
Current State
Active Rule
Condition Value
Derived Fact
19
Query Result
Execution Path
33. LOGIC GRAPH
המערכת צריכה להיות מסוגלת לייצגexecution graph:
Input
 ↓
Fact
 ↓
Rule
 ↓
Derived Fact
 ↓
Rule
 ↓
Decision
 ↓
Output
ובנפרד:
Rule Dependency Graph
Data Dependency Graph
Execution Graph
אלה אינם בהכרח אותוgraph.
34. META-PROGRAMMING
זהו אזור אסטרטגי.
הארכיטקטורה צריכה לאפשר בעתיד:
Code → inspect → transform → execute
ובטווח ארוך:
program
  ↓
20
analyze
  ↓
generate transformation
  ↓
validate
  ↓
produce program
אבל אין לאפשר לקוד לשנות קוד באופן בלתי מוגבל ב־MVP .
Meta-programming צריך להיותsandboxed ו־typed לפי הצורך.
35. SELF-ANALYSIS
חזון ארוך טווח:
המערכת תוכל לנתח לא רקdata אלא גם:
its own program structure
logic dependencies
rule activation
execution behavior
performance characteristics
חושבת כמו מוח אנושי "המטרה אינה לטעון שהמערכת."
המטרה ההנדסית היא לאפשרrepresentation מפורש ועשיר יותר שלcomputational reasoning.
36. AI INTEGRATION
AI אינו חלק מה־language semantics בגרסה הראשונה.
AI יכול להיותintegration layer:
Language
 ↓
Logic / Data
 ↓
AI Adapter
לדוגמה:
21
LLM input
→ structured result
→ facts
→ rules
→ reasoning
AI outputs חייבים לעבורvalidation לפני שהם הופכים ל־trusted logical state.
37. SECURITY ARCHITECTURE
המערכת מיועדת בעתיד ל־Enterprise ולכןsecurity אינהadd-on.
נדרשים:
Sandboxing
Permission Model
SQL Permissions
Filesystem Permissions
Network Permissions
Resource Limits
Execution Time Limits
Memory Limits
Secrets Management
Audit Logs
38. REPOSITORY
מבנה ראשוני:
logic-platform/
│
├── language/
│   ├── lexer/
│   ├── parser/
│   ├── ast/
│   ├── semantic/
│   ├── types/
│   └── grammar/
│
├── compiler/
│   ├── ir/
│   └── planner/
22
│
├── runtime/
│   ├── engine/
│   ├── state/
│   ├── facts/
│   ├── rules/
│   ├── reasoning/
│   └── trace/
│
├── data/
│   ├── sql/
│   ├── connectors/
│   └── permissions/
│
├── cli/
│
├── ide/
│
├── tests/
│
├── benchmarks/
│
├── examples/
│
└── docs/
39. DEVELOPMENT PHASES
PHASE 0 — LANGUAGE CONSTITUTION
לפני קוד:
להגדיר:
Syntax
Semantics
Types
Facts
Rules
State
Inference
Execution
Errors
Determinism
Conflicts
Cycles
23
Queries
Outputs
Output:
LANGUAGE_SPEC.md
SEMANTICS.md
EXECUTION_MODEL.md
PHASE 1 — MINIMAL LANGUAGE
לבנות רק:
Lexer
Parser
AST
Semantic Validation
בליGUI.
בליSQL.
בליAI.
בליinstaller .
PHASE 2 — LOGIC CORE
לבנות:
Facts
Rules
Conditions
State
Inference
Output
PHASE 3 — RUNTIME
לבנותruntime production-oriented.
24
PHASE 4 — IR
AST → IR → Runtime.
PHASE 5 — TRACE
להוסיף:
Execution Trace
Rule Trace
State Trace
Dependency Trace
PHASE 6 — SQL
להוסיףSQL abstraction + connector אחד תחילה.
PHASE 7 — CLI
להפוך את המערכת ל־developer-usable.
PHASE 8 — META LAYER
להוסיף בהדרגה:
Inspect Code
Inspect AST
Analyze Rules
Transform Program
רק לאחר שהcore semantics יציבים.
PHASE 9 — IDE
להוסיףGUI.
25
PHASE 10 — SECURITY
Sandbox + permissions + audit.
PHASE 11 — PERFORMANCE
רק לאחרcorrectness:
Benchmark
Profile
Optimize
Parallelize
Cache
PHASE 12 — PRODUCTION RELEASE
Build
Package
Sign
Install
Update
License
Observe
40. TESTING STRATEGY
Unit Tests
Lexer , parser , types, evaluator .
Property Tests
Language invariants.
Integration Tests
Compiler + runtime.
26
Logic Tests
Rules and inference.
SQL Tests
Database integration.
End-to-End
source
→ parse
→ semantic check
→ compile
→ runtime
→ SQL
→ reasoning
→ output
→ trace
Regression Tests
כלbug שמתגלה צריך לקבלregression test מתאים.
41. PERFORMANCE METRICS
יש למדוד:
Parse Time
Semantic Analysis Time
IR Generation Time
Execution Time
Rule Evaluation Time
SQL Latency
Memory Consumption
Throughput
Concurrency
Trace Overhead
אין לבצעoptimization על בסיס תחושה.
27
42. FORMAL VALIDATION
בשלב מתקדם יש לבחון:
Rule Consistency
Dependency Analysis
Reachability
Conflict Detection
Invariant Checking
Cycle Detection
התוכנית רצה "המטרה היא לא רק."
המטרה היא להיות מסוגלים להוכיח או לבדוקproperties מסוימים של הלוגיקה.
43. COMMERCIAL PRODUCT
המשתמש הסופי לא אמור לראות:
source repository
compiler internals
Rust toolchain
Python environment
development dependencies
הוא צריך לקבל:
Application
+
Runtime
+
Projects
+
Data Connections
+
Developer Tools
44. DISTRIBUTION
Windows target ראשון אפשרי.
28
Production package:
Application
Runtime
Required Libraries
Configuration
Updater
License Component
ולבסוףinstaller כגון:
Inno Setup
אבלinstaller לא חלק מ־, הוא שלב אחרוןlanguage development.
45. COMMERCIAL LICENSING
License subsystem עשוי לכלול:
License Key
Organization
Seats
Entitlements
Activation
Expiration
Feature Flags
אין להכניסlicensing לפני שהמערכת הפונקציונלית יציבה.
46. NON-GOALS FOR MVP
הMVP אינו צריך לכלול:
Full AI reasoning
Distributed execution
Cloud orchestration
Multiple database vendors
Advanced macros
Native JIT
Self-modifying unrestricted programs
Enterprise IAM
29
Billing
Advanced collaboration
אלא אם דרישה חדשה מוכיחה שהם חיוניים ל־core validation.
47. HARD DESIGN RULES
Rule 1
Do not invent semantics.
Rule 2
Do not silently change the language.
Rule 3
Do not mix prototype and production code.
Rule 4
Do not optimize before measurement.
Rule 5
Do not introduce a dependency without architectural justification.
Rule 6
Do not build UI before a stable core contract.
Rule 7
Do not add features because they "sound useful".
Rule 8
Every major feature requires:
Specification
Design
Implementation
30
Tests
Documentation
Rule 9
Every architectural assumption must be explicit.
Rule 10
When a decision is unspecified, mark:
OPEN DESIGN DECISION
Do not guess.
48. CLAUDE WORKING PROTOCOL
Claude should behave as a senior compiler engineer , programming-language designer , systems
engineer and logic-engineering researcher .
For every substantial task:
1. Understand existing specification.
2. Inspect repository.
3. Identify constraints.
4. Propose architecture.
5. Identify unresolved decisions.
6. Implement the smallest coherent change.
7. Run tests.
8. Inspect failures.
9. Fix root causes.
10. Update documentation.
11. Report exactly what changed.
Claude must not jump directly from request → code when the requested change affects language
semantics.
49. CLAUDE MUST DISTINGUISH THREE STATES
Every component must be classified as one of:
31
SPECIFIED
IMPLEMENTED
VERIFIED
Example:
Rule Conflict Semantics
SPECIFIED: yes
IMPLEMENTED: no
VERIFIED: no
Never report a component as completed merely because source code exists.
50. DEFINITION OF DONE — LANGUAGE CORE
The language core is complete only when:
[ ] Grammar defined
[ ] Lexer implemented
[ ] Parser implemented
[ ] AST stable
[ ] Semantic rules defined
[ ] Type system implemented
[ ] Facts implemented
[ ] Rules implemented
[ ] State model implemented
[ ] Inference semantics implemented
[ ] Execution semantics tested
[ ] Error system implemented
[ ] IR implemented
[ ] Runtime implemented
[ ] Regression tests exist
51. DEFINITION OF DONE — REASONING SYSTEM
[ ] Fact derivation works
[ ] Rule activation works
[ ] Dependencies are represented
[ ] Trace works
[ ] Explanation can be generated from trace
[ ] Cycles have defined behavior
32
[ ] Conflicts have defined behavior
[ ] Determinism is tested
52. DEFINITION OF DONE — SQL
[ ] SQL interface defined
[ ] Connector implemented
[ ] Query result mapping defined
[ ] Permissions defined
[ ] Errors mapped
[ ] Resource limits defined
[ ] SQL execution appears in trace
[ ] Integration tests pass
53. DEFINITION OF DONE — PRODUCT
[ ] Core
[ ] Compiler
[ ] Runtime
[ ] Logic Engine
[ ] Reasoning
[ ] SQL
[ ] CLI
[ ] IDE
[ ] Debugger
[ ] Security
[ ] Documentation
[ ] Installer
[ ] Signing
[ ] Licensing
[ ] Updates
54. OPEN DESIGN DECISIONS
The following decisions must be explicitly resolved before their implementation:
Language Name
Exact Grammar
Syntax Style
33
Evaluation Strategy
Rule Scheduling
Conflict Resolution
Cycle Semantics
Recursion Semantics
Mutation Model
Side Effect Model
Concurrency Model
Type System Details
Memory Model
Query Semantics
Meta-Programming Model
Macro System
AI Integration Boundary
Plugin Architecture
Distributed Execution
No agent may silently choose values for these decisions.
55. INITIAL DESIGN QUESTIONS
The next design phase must answer these questions in order:
A. What exactly is a Rule?
Is it:
constraint?
implication?
procedure?
trigger?
relation?
B. What exactly is a Fact?
Is it:
immutable knowledge?
mutable state?
typed proposition?
record?
C. What is Inference?
Is inference:
34
forward chaining?
backward chaining?
hybrid?
fixed point?
search?
D. What is State?
What can change?
E. What is the unit of execution?
program?
rule set?
transaction?
query?
goal?
F. How does SQL enter the logical model?
query → facts?
relation → first-class value?
external source?
G. What can code know about itself?
AST only?
IR?
runtime state?
execution history?
other code?
These questions define the actual identity of the language.
56. FIRST IMPLEMENTATION TARGET
The first real implementation should be intentionally small.
Target:
source file
 ↓
lexer
35
 ↓
parser
 ↓
AST
 ↓
semantic validation
 ↓
simple rule engine
 ↓
facts
 ↓
derived facts
 ↓
output
Example conceptual program:
fact temperature = 31
rule heat:
    when temperature > 30
    then status = "hot"
output status
Expected result:
status = "hot"
And the runtime should also be able to produce:
temperature = 31
→ rule heat activated
→ condition true
→ status derived
→ output generated
This is a vertical slice, not the final language.
57. THE FIRST REAL MILESTONE
The first milestone is not:
36
GUI
and not:
commercial installer
The first milestone is:
A small but formally defined language that can express facts and rules, execute them
deterministically, produce derived facts and output, and expose a machine-readable
reasoning trace.
Only after that milestone is correct should SQL and meta-programming become major implementation
targets.
58. FINAL MISSION
The project is attempting to create a new computational abstraction in which:
Code
+
Logic
+
Knowledge
+
Data
+
Reasoning
+
Execution
are represented in one coherent system.
The objective is not to make a faster Python.
The objective is not to copy Prolog.
The objective is not to build an AI chatbot.
The objective is to design a new language/runtime architecture whose primitives make logic, rules,
reasoning, data and code structure explicit and executable.
The value of the project must ultimately be demonstrated through:
37
Correctness
Expressiveness
Performance
Observability
Composability
Research Utility
Developer Productivity
Enterprise Reliability
Not through claims of superiority.
59. DIRECT INSTRUCTION TO CLAUDE
You are not being asked to immediately write the entire system.
You are being asked to engineer it from first principles.
Treat this specification as the current source of truth.
Your responsibilities:
Understand
→ Formalize
→ Challenge assumptions
→ Design
→ Implement
→ Test
→ Measure
→ Refine
When the specification is ambiguous:
DO NOT GUESS
instead write:
OPEN DESIGN DECISION
When an architectural choice appears weak:
DO NOT blindly implement it.
38
Explain the technical problem and propose alternatives.
When code exists:
DO NOT assume it works.
Run tests and verify behavior .
When a feature is implemented:
DO NOT call it complete
until its tests and acceptance criteria pass.
The objective is a real programming-language/runtime product, not a mockup, demo, pseudo-compiler
or folder containing illustrative code.
60. REQUIRED NEXT OUTPUT FROM CLAUDE
Before writing production implementation code, Claude must produce these five design artifacts:
01_LANGUAGE_CONSTITUTION.md
02_FORMAL_SEMANTICS.md
03_EXECUTION_MODEL.md
04_TYPE_AND_DATA_MODEL.md
05_ARCHITECTURE.md
Only after those documents are internally consistent should implementation begin.
The first implementation should then target the smallest vertical slice:
Source
→ Lexer
→ Parser
→ AST
→ Semantic Validation
→ Rule Engine
→ Runtime
→ Output
→ Trace
Every later subsystem must connect to this foundation rather than bypass it.
39