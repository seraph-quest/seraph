"""Finite local spreadsheet grammar and arithmetic; never imported evaluation."""
from __future__ import annotations
import math
import re
from .document_build_contracts import SpreadsheetSpec, coordinate

TOKEN = re.compile(r'\s*(?:(\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|("(?:[^"]|"")*")|([A-Za-z_$][A-Za-z0-9_$]*)|(<>|<=|>=|[+*/^%(),:!<>=-]))')
FUNCTIONS = {'SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'IF'}
FORMULA_ERROR_CODES = frozenset({
    'document_formula_arguments_invalid', 'document_formula_arithmetic_invalid',
    'document_formula_comparison_type', 'document_formula_cycle',
    'document_formula_dependency_depth_bound', 'document_formula_depth_bound',
    'document_formula_empty_numeric_range', 'document_formula_exponent_bound',
    'document_formula_extra_tokens', 'document_formula_foreign_sheet',
    'document_formula_function_unsupported', 'document_formula_if_boolean_required',
    'document_formula_incomplete', 'document_formula_numeric_value_required',
    'document_formula_operation_bound', 'document_formula_range_bound',
    'document_formula_range_invalid', 'document_formula_reference_invalid',
    'document_formula_syntax', 'document_formula_token_invalid',
})


class FormulaError(ValueError):
    def __init__(self, code, *, sheet=None, cell=None):
        if code not in FORMULA_ERROR_CODES:
            raise ValueError('document_formula_code_invalid')
        self.code, self.sheet, self.cell = code, sheet, cell
        super().__init__(f'{sheet}!{cell}: {code}' if sheet is not None else code)


def located_error(error, sheet, cell):
    # Only parser-owned finite codes cross the validation boundary.
    code = error.code if isinstance(error, FormulaError) else 'document_formula_reference_invalid'
    if isinstance(error, FormulaError) and error.sheet is not None:
        return error
    return FormulaError(code, sheet=sheet, cell=cell.replace('$', ''))


class Parser:
    def __init__(self, expression, sheet, sheets):
        text = expression[1:] if expression.startswith('=') else expression
        self.tokens = []
        offset = 0
        while offset < len(text):
            match = TOKEN.match(text, offset)
            if not match:
                if not text[offset:].strip():
                    break
                raise FormulaError('document_formula_token_invalid')
            self.tokens.append(next(value for value in match.groups() if value is not None))
            offset = match.end()
        self.index = 0
        self.sheet, self.sheets = sheet, sheets
        self.range_cells = 0

    def take(self, expected=None):
        if self.index >= len(self.tokens):
            raise FormulaError('document_formula_incomplete')
        token = self.tokens[self.index]
        if expected and token != expected:
            raise FormulaError('document_formula_syntax')
        self.index += 1
        return token

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def reference(self, sheet, token):
        if sheet not in self.sheets:
            raise FormulaError('document_formula_foreign_sheet')
        try:
            row, col = coordinate(token)
        except ValueError:
            raise FormulaError('document_formula_reference_invalid') from None
        if self.peek() != ':':
            return ('ref', (sheet, row, col))
        self.take(':')
        endrow, endcol = coordinate(self.take())
        if endrow < row or endcol < col:
            raise FormulaError('document_formula_range_invalid')
        keys = tuple((sheet, r, c) for r in range(row, endrow + 1) for c in range(col, endcol + 1))
        self.range_cells += len(keys)
        return ('range', keys)

    def parse(self, minimum=0, depth=0):
        if depth > 32:
            raise FormulaError('document_formula_depth_bound')
        token = self.take()
        if token in ('+', '-'):
            node = ('unary', token, self.parse(35, depth + 1))
        elif token == '(':
            node = self.parse(0, depth + 1)
            self.take(')')
        elif token.startswith('"'):
            node = ('literal', token[1:-1].replace('""', '"'))
        elif token[0].isdigit() or token[0] == '.':
            node = ('literal', float(token))
        elif self.peek() == '(':
            name = token.upper()
            if name not in FUNCTIONS:
                raise FormulaError('document_formula_function_unsupported')
            self.take('(')
            args = []
            if self.peek() != ')':
                while True:
                    args.append(self.parse(0, depth + 1))
                    if self.peek() != ',':
                        break
                    self.take(',')
            self.take(')')
            if (name == 'IF' and len(args) != 3) or (name != 'IF' and not args):
                raise FormulaError('document_formula_arguments_invalid')
            node = ('function', name, args)
        elif self.peek() == '!':
            self.take('!')
            node = self.reference(token, self.take())
        elif token.upper() in ('TRUE', 'FALSE'):
            node = ('literal', token.upper() == 'TRUE')
        else:
            node = self.reference(self.sheet, token)
        priorities = {'=': 10, '<>': 10, '<': 10, '>': 10, '<=': 10, '>=': 10, '+': 20, '-': 20, '*': 30, '/': 30, '^': 40, '%': 50}
        while self.peek() in priorities and priorities[self.peek()] >= minimum:
            op = self.take()
            if op == '%':
                node = ('unary', '%', node)
            else:
                node = ('binary', op, node, self.parse(priorities[op] + (0 if op == '^' else 1), depth + 1))
        return node


def references(node):
    if node[0] == 'ref':
        return [node[1]]
    if node[0] == 'range':
        return node[1]
    if node[0] == 'unary':
        return references(node[2])
    if node[0] == 'binary':
        return [*references(node[2]), *references(node[3])]
    if node[0] == 'function':
        return [ref for arg in node[2] for ref in references(arg)]
    return []


def calculate(spec: SpreadsheetSpec):
    values = {(cell.sheet, *coordinate(cell.cell)): cell.value for cell in spec.cells}
    trees = {}
    locations = {}
    expansions = 0
    for formula in spec.formulas:
        try:
            parser = Parser(formula.expression, formula.sheet, spec.sheet_names)
            tree = parser.parse()
            if parser.peek() is not None:
                raise FormulaError('document_formula_extra_tokens')
            expansions += parser.range_cells
            if expansions > 16384:
                raise FormulaError('document_formula_range_bound')
            key = (formula.sheet, *coordinate(formula.cell))
            trees[key] = tree
            locations[key] = (formula.sheet, formula.cell)
        except ValueError as exc:
            raise located_error(exc, formula.sheet, formula.cell) from None
    visiting, visited = set(), set()
    def visit(key, depth=0):
        if depth > 256:
            raise FormulaError('document_formula_dependency_depth_bound')
        if key in visiting:
            raise FormulaError('document_formula_cycle')
        if key in visited or key not in trees:
            return
        visiting.add(key)
        for ref in references(trees[key]):
            visit(ref, depth + 1)
        visiting.remove(key)
        visited.add(key)
    for key in trees:
        try:
            visit(key)
        except ValueError as exc:
            raise located_error(exc, *locations[key]) from None
    operations = 0
    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
            raise FormulaError('document_formula_numeric_value_required')
        return value
    def cell(key):
        if key not in values and key in trees:
            values[key] = evaluate(trees[key])
        return values.get(key)
    def evaluate(node):
        nonlocal operations
        operations += 1
        if operations > 100000:
            raise FormulaError('document_formula_operation_bound')
        kind = node[0]
        if kind == 'literal':
            if type(node[1]) is float:
                number(node[1])
            return node[1]
        if kind == 'ref':
            return cell(node[1])
        if kind == 'range':
            return [cell(key) for key in node[1]]
        if kind == 'unary':
            value = number(evaluate(node[2]))
            return number(value if node[1] == '+' else -value if node[1] == '-' else value / 100)
        if kind == 'binary':
            op, a, b = node[1], evaluate(node[2]), evaluate(node[3])
            if op in ('=', '<>', '<', '>', '<=', '>='):
                if not (type(a) == type(b) or type(a) in (int, float) and type(b) in (int, float)) or isinstance(a, list) or a is None:
                    raise FormulaError('document_formula_comparison_type')
                return {'=': lambda: a == b, '<>': lambda: a != b, '<': lambda: a < b, '>': lambda: a > b, '<=': lambda: a <= b, '>=': lambda: a >= b}[op]()
            a, b = number(a), number(b)
            try:
                if op == '^' and abs(b) > 1024:
                    raise FormulaError('document_formula_exponent_bound')
                value = {'+': lambda: a+b, '-': lambda: a-b, '*': lambda: a*b, '/': lambda: a/b, '^': lambda: a**b}[op]()
                return number(value)
            except (ArithmeticError, TypeError):
                raise FormulaError('document_formula_arithmetic_invalid') from None
        name, args = node[1], node[2]
        if name == 'IF':
            condition = evaluate(args[0])
            if type(condition) is not bool:
                raise FormulaError('document_formula_if_boolean_required')
            return evaluate(args[1] if condition else args[2])
        flattened = []
        for arg in args:
            value = evaluate(arg)
            flattened.extend(value if isinstance(value, list) else [value])
        numbers = [number(value) for value in flattened if type(value) in (int, float)]
        if name == 'COUNT':
            return len(numbers)
        if name == 'SUM':
            return number(sum(numbers))
        if not numbers:
            raise FormulaError('document_formula_empty_numeric_range')
        return number(sum(numbers)/len(numbers) if name == 'AVERAGE' else min(numbers) if name == 'MIN' else max(numbers))
    for key in trees:
        try:
            cell(key)
        except ValueError as exc:
            raise located_error(exc, *locations[key]) from None
    return values
